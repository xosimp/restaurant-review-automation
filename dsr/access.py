"""
dsr.access — who sees which DSR. One set of facts, two views.

Owner vs Manager is a permission view, not a second generation (plan §2):
every view — the app, the emails and pushes to come, Ask — renders from the
same stored facts through this module, so no two surfaces can disagree about
what a login may read.

  view_for(user)   "owner" for an owner login at the location (the account
                   holders — permissions.is_principal: the owner and client
                   roles, and Cavnar admins), "manager" for every other
                   console login (manager, member), None for anyone with no
                   console at all (employees, support). Will's rule: every
                   owner login gets the Owner DSR, every manager login the
                   Manager DSR.

What a view leaves out, using the rules permissions.py already has — never a
new one:

  * a whole block the login's role cannot read: labor → LABOR_VIEW, food →
    FOOD_COST_VIEW, reviews → REVIEWS_VIEW, marketing → MARKETING_VIEW,
    intel → INTEL_VIEW (permissions.MODULE_VIEW_PERMISSIONS). A manager
    therefore gets no Food block unless the owner granted Food cost.
  * comps, voids, refunds and anything named loss* → LOSS_VIEW (the
    comps-and-voids permission an owner may grant a manager).
  * owner-only financials — anything named budget* / vs_budget*,
    prime_cost*, and the POS reconciliation (source_checks) — in the owner
    view only. There is no grant for these.

Those name rules are the contract for every block: a collector that names an
owner-only line this way is redacted without this module knowing its block.

The narrative is filtered by what it cites. Each prose item is
{"text": ..., "cites": ["<block>.<key>", ...]} (dsr.narrative's shape; the
older "facts" name is read too); an item citing anything the
view withholds is dropped, and when a view withholds anything at all, prose
that cites nothing is dropped too — an uncited sentence cannot be shown to
be safe. `meta` passes through untouched. Where the executive summary is
withheld (it usually cites the budget) the narrative's manager-safe
operations_summary becomes the lead (narrative_for), so the Manager DSR is
never without an opening when one could be written.
"""
import copy

import dsr

OWNER = "owner"
MANAGER = "manager"

OWNER_ONLY_PREFIXES = ("budget", "vs_budget", "prime_cost", "source_checks")
LOSS_KEYS = ("comps", "voids", "refunds")
# Figures that close the net equation (D2-2): net = gross − discounts −
# comps, and on the "everything rung" basis gross = items + tax + voids — so
# a login shown gross, gross_items, discounts, tax and net could subtract
# its way to the comps and voids it was not granted. Without LOSS_VIEW the
# gross figures go too; net, discounts and tax alone derive nothing.
LOSS_DERIVED_KEYS = ("gross", "gross_items")

STAGE_LABELS = {
    "scheduled": "Scheduled",
    "awaiting_close": "Closeout in progress — waiting for the POS to close the day",
    "collecting": "Collecting",
    "writing": "Writing the summary",
    "final": "Ready",
    "provisional": "Provisional — some data is still syncing",
    "failed": "Couldn't finish tonight",
}
BLOCK_LABELS = {"sales": "Sales", "labor": "Labor", "food": "Food", "reviews": "Reviews",
                "marketing": "Marketing", "intel": "Intel", "closeout": "Manager closeout"}


def _block_permissions():
    from permissions import (LABOR_VIEW, FOOD_COST_VIEW, REVIEWS_VIEW, MARKETING_VIEW, INTEL_VIEW)
    return {"labor": LABOR_VIEW, "food": FOOD_COST_VIEW, "reviews": REVIEWS_VIEW,
            "marketing": MARKETING_VIEW, "intel": INTEL_VIEW}


def view_for(user):
    """"owner", "manager", or None (no DSR at all). The one decision every
    DSR surface makes — reuse it, never re-derive it from a role string."""
    from permissions import DASHBOARD_ACCESS, has_permission, is_principal
    if not user:
        return None
    if is_principal(user):
        return OWNER
    if has_permission(user, DASHBOARD_ACCESS):
        return MANAGER
    return None


def _sees(user, permission):
    from permissions import has_permission
    return bool(user and (user.get("is_admin") or has_permission(user, permission)))


def line_allowed(user, view, key):
    """Whether a metric or detail key may be shown in this view."""
    from permissions import LOSS_VIEW
    k = str(key).lower()
    if k.startswith(OWNER_ONLY_PREFIXES) and view != OWNER:
        return False
    if (k in LOSS_KEYS or k in LOSS_DERIVED_KEYS or k.startswith("loss")) and not _sees(user, LOSS_VIEW):
        return False
    return True


def redact(facts, user):
    """(facts for this login, hidden) — `hidden` is the set of "<block>.<key>"
    and "<block>." citations the view withholds, for filtering the
    narrative."""
    view = view_for(user)
    out = copy.deepcopy(facts or {})
    hidden, withheld = set(), []
    perms = _block_permissions()
    blocks = out.get("blocks") or {}
    for name in list(blocks):
        need = perms.get(name)
        if view is None or (need and not _sees(user, need)):
            blocks.pop(name)
            withheld.append(name)
            hidden.add(f"{name}.")
            continue
        b = blocks[name]
        for part in ("metrics", "detail"):
            section = b.get(part) or {}
            for key in list(section):
                if not line_allowed(user, view, key):
                    section.pop(key)
                    hidden.add(f"{name}.{key}")
    for name, need in perms.items():
        # A block this login may not read is hidden whether or not tonight
        # collected it — a line naming its subject is still not theirs.
        if view is None or (need and not _sees(user, need)):
            hidden.add(f"{name}.")
    out["blocks"] = blocks
    out["missing"] = dsr.missing_reasons(blocks)
    out["withheld"] = [n for n in dsr.BLOCKS if n in withheld]
    # The stored next-night snapshot carries the predictions' raw values
    # (`_preds`: the budget) and every stock line: it is read ONLY through
    # tomorrow_for, per view — never passed through whole (D3-2).
    out.pop("tomorrow", None)
    return out, hidden


def _cites_hidden(cites, hidden):
    for c in cites or ():
        c = str(c)
        for h in hidden:
            if c == h or (h.endswith(".") and c.startswith(h)) or c.startswith(h + ".") or c.startswith(h + ":"):
                return True
    return False


_TOPIC_WORDS = {
    "budget": r"budget\w*",
    "prime": r"prime[\s-]+cost",
    "food": r"food[\s-]+cost|cogs",
    "loss": r"comps?|comped|voids?|voided|refunds?|refunded|loss(?:es)?|shrink",
}


def _hidden_topics(hidden):
    """The words a line may not use in this view, as one regex, or None:
    the subjects of what the view withholds — the budget (and prime cost)
    for any view without the owner's financials, food cost without the Food
    block, comps/voids/refunds without LOSS_VIEW. The same subjects
    dsr.narrative._OWNER_TOPIC_RE keeps out of the operations summary."""
    import re
    words = []
    hs = {str(h) for h in hidden or ()}
    if any(h.startswith(("sales.budget", "sales.vs_budget")) for h in hs):
        words += [_TOPIC_WORDS["budget"], _TOPIC_WORDS["prime"]]
    if "food." in hs:
        words.append(_TOPIC_WORDS["food"])
    if any(h.split(".", 1)[-1] in LOSS_KEYS for h in hs):
        words.append(_TOPIC_WORDS["loss"])
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None


def filter_narrative(narrative, hidden):
    """The narrative with every item that cites a withheld fact removed —
    and, when anything is withheld, every uncited sentence too."""
    if not isinstance(narrative, dict):
        return None
    if not hidden:
        return copy.deepcopy(narrative)

    topics = _hidden_topics(hidden)

    def keep(node, cited=False):
        if isinstance(node, dict):
            refs = node.get("cites") if isinstance(node.get("cites"), list) else node.get("facts")
            if isinstance(refs, list):
                if _cites_hidden(refs, hidden):
                    return None
                # A line that names a withheld subject in words, with no
                # figure to cite ("Sales came in under budget again."), is
                # as much the owner's as one that cites it (D2-5).
                if topics is not None and isinstance(node.get("text"), str) and topics.search(node["text"]):
                    return None
                cited = True
            out = {}
            for k, v in node.items():
                if k == "meta":
                    out[k] = copy.deepcopy(v)
                    continue
                kept = keep(v, cited)
                if kept is not None:
                    out[k] = kept
            return out or None
        if isinstance(node, list):
            items = [x for x in (keep(v, cited) for v in node) if x is not None]
            return items
        if isinstance(node, str):
            return node if cited else None
        return node

    return keep(narrative) or {}


def narrative_for(narrative, hidden):
    """The narrative this view reads: filter_narrative, then the lead. The
    executive summary usually cites the budget, so a manager's copy lost its
    opening entirely; where it is gone and the narrative's operations_summary
    (dsr.narrative — operations only, manager-safe by construction) survived,
    that is the lead. `executive_summary` is therefore always the lead to
    show, and `lead_from` says when it was substituted."""
    n = filter_narrative(narrative, hidden)
    if isinstance(n, dict) and n.get("operations_summary") and n.get("executive_summary") != n["operations_summary"] \
            and any(str(h).startswith("sales.budget") for h in hidden):
        # The Manager DSR leads with its own operations summary (9/25/26:
        # "every report has one", and the manager's is operations, not
        # finance) — the executive summary is the owner's.
        n["executive_summary"] = n["operations_summary"]
        n["lead_from"] = "operations_summary"
    if isinstance(n, dict) and n.get("largest_money_saving"):
        # Retired (dsr.narrative.RETIRED_SINGLES, NS3 C2): a stored night's
        # "Largest saving" line named an opportunity or a budget miss as
        # money saved. Never shown again, on any stored report.
        n["largest_money_saving"] = None
    if isinstance(n, dict) and not n.get("executive_summary") and n.get("operations_summary"):
        n["executive_summary"] = n["operations_summary"]
        n["lead_from"] = "operations_summary"
    if hidden and isinstance(n, dict) and isinstance(n.get("verification"), dict):
        try:
            n["verification"] = _recount(n)
        except Exception:
            n.pop("verification", None)     # no count at all rather than the owner's
    return n


def _recount(n):
    """The verification footer for a filtered view (D2-8): counted over the
    lines this view SHOWS — "7 of 9 lines kept" over a narrative showing a
    manager three lines described the owner's copy, not theirs. The lines
    verification dropped were never shown to anyone; the count here is of
    what is on the page, each line once (the substituted lead is the
    operations summary, not a second line)."""
    from dsr.narrative import ITEM_LISTS, ITEM_SINGLES, line_kind
    items = [n.get("executive_summary")]
    if n.get("operations_summary") and n.get("operations_summary") != n.get("executive_summary"):
        items.append(n.get("operations_summary"))
    items += [it for f in ITEM_LISTS for it in (n.get(f) or [])]
    items += [n.get(f) for f in ITEM_SINGLES]
    items += list(n.get("actions_tomorrow") or [])
    items = [it for it in items if isinstance(it, dict) and it.get("text")]
    by_kind = {k: 0 for k in ("measured", "estimate", "opportunity", "plan")}
    for it in items:
        by_kind[line_kind(it.get("cites") if isinstance(it.get("cites"), list) else it.get("facts"))] += 1
    v = dict(n.get("verification") or {})
    v.update({"checked": len(items), "kept": len(items), "dropped": [], "failed_check": 0,
              "withheld_answered": 0, "estimated": by_kind["estimate"], "opportunity": by_kind["opportunity"],
              "plan": by_kind["plan"], "by_kind": by_kind, "measured": by_kind["measured"],
              "counted": "shown in this view"})
    return v


def metric_allowed(user, metric):
    """Whether a "<block>.<key>" metric (a dsr_metrics row) may be read by
    this login — the same block and line rules redact() applies, for the
    surfaces that read metrics outside a report (Ask's find_days)."""
    view = view_for(user)
    if view is None:
        return False
    block, _, key = str(metric).partition(".")
    need = _block_permissions().get(block)
    if need and not _sees(user, need):
        return False
    return line_allowed(user, view, key)


def _stamp_local(stamp, restaurant):
    """A stored UTC stamp as local wall clock ("YYYY-MM-DDTHH:MM"), or None."""
    if not stamp or restaurant is None:
        return None
    from dsr.pipeline import local_time, _parse_utc
    at = _parse_utc(stamp)
    return local_time(restaurant, at).strftime("%Y-%m-%dT%H:%M") if at else None


def checklist(report, user, restaurant=None):
    """The progressive stage checklist for the app: stages in order with the
    time each was reached, each visible block with its status and time, the
    narrative's outcome, and when the pipeline looks again."""
    from time_utils import mdy
    stages = report.get("stages") or {}
    status = report.get("status")
    path = ["awaiting_close", "collecting", "writing"] + [status if status in dsr.TERMINAL_STAGES else "final"]
    rows = []
    for key in ["scheduled"] + path:
        at = stages.get(key)
        rows.append({"key": key, "label": STAGE_LABELS[key], "at": at, "at_local": _stamp_local(at, restaurant),
                     "done": bool(at) and key != status or (key == status and status in dsr.TERMINAL_STAGES),
                     "current": key == status})
    facts, _hidden = redact(report.get("facts") or {}, user)
    stamps = stages.get("blocks") or {}
    blocks = []
    for name in dsr.BLOCKS:
        b = (facts.get("blocks") or {}).get(name)
        if b is None:
            if name in facts.get("withheld", []):
                continue
            blocks.append({"name": name, "label": BLOCK_LABELS[name], "status": None, "reason": None,
                           "at": None, "at_local": None})
            continue
        at = stamps.get(name)
        blocks.append({"name": name, "label": BLOCK_LABELS[name], "status": b.get("status"),
                       "reason": b.get("reason"), "at": at, "at_local": _stamp_local(at, restaurant)})
    day = report.get("business_date")
    return {"business_date": day, "label": mdy(day), "version": report.get("version"), "status": status,
            "status_label": STAGE_LABELS.get(status, status), "provisional": bool(report.get("provisional")),
            "stages": rows, "blocks": blocks, "narrative": stages.get("narrative"),
            "closed_by": stages.get("closed_by"), "next_attempt_at": report.get("next_attempt_at"),
            "missing": facts.get("missing") or [],
            # A re-run the pipeline refused because the POS didn't return the
            # night's sales (D1-12): {"at", "refused", "why"} — the report
            # shown is unchanged, and this says why no new version came.
            "rerun": stages.get("rerun")}


def render(report, user, restaurant=None, versions=None):
    """The whole report for this login: facts, narrative and checklist, all
    from the one stored snapshot."""
    from time_utils import mdy
    view = view_for(user)
    stored = report.get("facts") or {}
    if view == OWNER:
        stored = _live_budget(stored, report, restaurant)
    facts, hidden = redact(stored, user)
    # "Did we win today?" — the owner's scorecard (dsr.scorecard): score,
    # wins and risks, all from the measured blocks. The manager's view keeps
    # its own layout: the budget and the food estimate are the owner's.
    card = None
    if view == OWNER:
        try:
            from dsr import scorecard
            card = scorecard.build(stored, restaurant, narrative=report.get("narrative"))
        except Exception:
            card = None
    # Every figure with direction and, where fair, a benchmark (dsr.kpis);
    # the manager's set is operations, never finance.
    try:
        from dsr import kpis as _kpis
        kp = _kpis.build(facts, restaurant, user, view)
    except Exception:
        kp = {"top": [], "operations": [], "shift": None}
    return {
        "view": view,
        "business_date": report.get("business_date"),
        "label": mdy(report.get("business_date")),
        "fiscal": facts.get("fiscal"),
        "version": report.get("version"),
        "status": report.get("status"),
        "provisional": bool(report.get("provisional")),
        "trigger": report.get("trigger"),
        "finalized_at": report.get("finalized_at"),
        "facts": facts,
        "narrative": narrative_for(report.get("narrative"), hidden),
        "scorecard": card,
        "kpis": kp.get("top") or [],
        "operations": kp.get("operations") or [],
        "shift": kp.get("shift"),
        "tomorrow": tomorrow_for(stored, user, view, withheld=facts.get("withheld")),
        "insights": insights(facts, report.get("narrative") and narrative_for(report.get("narrative"), hidden), view),
        "yesterday": _yesterday(report, restaurant, user, view),
        "checklist": checklist(report, user, restaurant),
        "versions": [dict(v, finalized_at_local=_stamp_local(v.get("finalized_at"), restaurant),
                          created_at_local=_stamp_local(v.get("created_at"), restaurant))
                     for v in (versions or [])],
    }


def tomorrow_for(facts, user, view, withheld=None):
    """The stored next-night snapshot (dsr.tomorrow) as this login may read
    it: a login without the Food view loses the stock lines, and a
    prediction resting on a fact the view withholds (the budget) is not
    listed (D2-1). `facts` is the STORED facts (redact() drops the raw
    snapshot); `withheld` the view's withheld blocks."""
    t = (facts or {}).get("tomorrow")
    if not isinstance(t, dict):
        return None
    from dsr import predictions
    t = copy.deepcopy(t)
    t.pop("_preds", None)
    if withheld is None:
        withheld = [n for n, need in _block_permissions().items() if view is None or not _sees(user, need)]
    if "food" in (withheld or []):
        t["items"] = [i for i in t.get("items") or [] if i.get("kind") != "stock"]
    ok = _cite_rule(user, view)
    t["predictions"] = [p for p in t.get("predictions") or []
                        if isinstance(p, dict) and all(ok(c) for c in predictions.cites_for(p.get("key")))]
    return t


def _cite_rule(user, view):
    """cite -> whether this view may read that "<block>.<key>" fact."""
    perms = _block_permissions()

    def ok(cite):
        block, _, key = str(cite).partition(".")
        need = perms.get(block)
        if view is None or (need and not _sees(user, need)):
            return False
        return line_allowed(user, view, key)
    return ok


def insights(facts, narrative, view):
    """AI insights: the narrative's verified callouts (biggest win, risk,
    staffing concern …) and — owner only — the money the Food block counts
    at stake. An opportunity is never money saved (value_delivered rules):
    it is labelled at stake, a month, and its basis."""
    out = []
    n = narrative if isinstance(narrative, dict) else {}
    labels = (("biggest_win", "Biggest win"), ("biggest_risk", "Biggest risk"),
              ("highest_priority_issue", "Top priority"),
              ("biggest_financial_opportunity", "Biggest opportunity"), ("largest_opportunity", "Biggest opportunity"),
              ("biggest_staffing_concern", "Staffing"), ("largest_staffing", "Staffing"),
              ("largest_guest_experience", "Guests"))
    seen = set()
    for key, label in labels:
        item = n.get(key)
        text = item.get("text") if isinstance(item, dict) else None
        if text and text not in seen:
            seen.add(text)
            out.append({"kind": key, "label": label, "text": text, "source": "narrative"})
    if view == OWNER:
        food = ((facts or {}).get("blocks") or {}).get("food") or {}
        fm = food.get("metrics") or {} if food.get("status") == dsr.READY else {}
        at = fm.get("drivers_at_stake_monthly")
        if isinstance(at, (int, float)) and at > 0:
            out.append({"kind": "at_stake", "label": "Money at stake",
                        "text": f"${at:,.0f} a month across your food-cost drivers — at stake, not saved",
                        "source": "measured"})
    return out


def _yesterday(report, restaurant, user=None, view=OWNER):
    """"How did yesterday turn out?" — what the previous night's report
    predicted about this night, graded (dsr.predictions), as this view may
    read it: "Sales expected above budget ($9,500)" is the owner's (D2-1),
    and the accuracy counts only what the view is shown."""
    try:
        from dsr import predictions
        rid = report.get("restaurant_id") or getattr(restaurant, "id", None)
        return predictions.review(rid, report.get("business_date"), allowed=_cite_rule(user, view)) if rid else None
    except Exception:
        return None


def _live_budget(facts, report, restaurant):
    """The owner's facts with the night's budget as it stands NOW (D1-14).
    The report freezes the budget at collection, but Erik sets a day's
    budget the next morning: the weekly grid read it and the report said
    "No budget". Where the budget table now differs from what the Sales
    block froze, its budget and vs-budget figures are re-read from it (the
    scorecard with them), and detail.budget says it was entered after the
    report. Owner view only — the budget is the owner's."""
    sales = ((facts or {}).get("blocks") or {}).get("sales") or {}
    if sales.get("status") != dsr.READY:
        return facts
    try:
        from dsr import store
        rid = report.get("restaurant_id") or getattr(restaurant, "id", None)
        day = str(report.get("business_date"))[:10]
        live = store.budgets_for(rid, day, day).get(day) or {}
    except Exception:
        return facts
    m = sales.get("metrics") or {}
    b_gross, b_net = live.get("gross"), live.get("net")

    def same(a, b):
        return (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 0.005)
    if not live or (same(b_gross, m.get("budget_gross")) and same(b_net, m.get("budget_net"))):
        return facts
    from dsr.block_sales import _delta, _pct
    out = copy.deepcopy(facts)
    s = out["blocks"]["sales"]
    sm = s.setdefault("metrics", {})
    gross, net = sm.get("gross"), sm.get("net")
    sm.update({"budget_gross": b_gross, "vs_budget_gross": _delta(gross, b_gross),
               "vs_budget_gross_pct": _pct(gross, b_gross),
               "budget_net": b_net, "vs_budget_net": _delta(net, b_net), "vs_budget_net_pct": _pct(net, b_net)})
    s.setdefault("detail", {})["budget"] = {"gross": b_gross, "net": b_net, "entered_after_report": True}
    return out


def summary(report, user):
    """One row of the report list — with what Home's "Last night" card
    shows: the night's net sales when the Sales block is ready (None
    otherwise, never 0), the narrative's lead as this login may read it, and
    when there is no lead, the reason the summary wasn't written."""
    from time_utils import mdy
    facts, hidden = redact(report.get("facts") or {}, user)
    sales = (facts.get("blocks") or {}).get("sales") or {}
    net = (sales.get("metrics") or {}).get("net") if sales.get("status") == dsr.READY else None
    # The same lead the report view opens with (D2-4): narrative_for, so a
    # manager's list row and Home card lead with the operations summary
    # exactly as their report does.
    narrative = narrative_for(report.get("narrative"), hidden) or {}
    lead = narrative.get("executive_summary") if isinstance(narrative, dict) else None
    lead_text = lead.get("text") if isinstance(lead, dict) else None
    note = (report.get("stages") or {}).get("narrative") or {}
    return {"business_date": report.get("business_date"), "label": mdy(report.get("business_date")),
            "version": report.get("version"), "status": report.get("status"),
            "provisional": bool(report.get("provisional")), "missing": facts.get("missing") or [],
            "finalized_at": report.get("finalized_at"), "net": net, "lead": lead_text,
            "lead_missing": None if lead_text else lead_missing(report.get("narrative"), note)}


NO_LEAD_FOR_VIEW = "The summary rests on figures outside your view"


def lead_missing(stored_narrative, note):
    """Why a view has no lead: the pipeline's reason when no summary was
    written, else — a summary exists but none of it is this view's — say
    that, never nothing (D2-4)."""
    if isinstance(stored_narrative, dict) and stored_narrative:
        return NO_LEAD_FOR_VIEW
    return note.get("reason") if isinstance(note, dict) else None


def redact_grid(grid, user):
    """A week or period from dsr.rollup, as this login may read it: the
    budget columns (and their comparisons) are the owner's, as on the
    nightly report — and the labor columns go too for a login without
    LABOR_VIEW, as the nightly report drops its Labor block. Returns a copy;
    None passes through."""
    from permissions import LABOR_VIEW, LOSS_VIEW
    if grid is None:
        return None
    if view_for(user) == OWNER:
        return grid
    # Gross goes with the loss lines for a login without LOSS_VIEW (D2-2):
    # beside net it is the comps (and on the "everything rung" basis, voids).
    gone = OWNER_ONLY_PREFIXES + (() if _sees(user, LABOR_VIEW) else ("labor",)) \
        + (() if _sees(user, LOSS_VIEW) else ("gross",))

    def strip(d):
        return {k: v for k, v in d.items() if not str(k).startswith(gone)}

    g = copy.deepcopy(grid)
    for key in ("days",):
        g[key] = [strip(r) for r in g.get(key) or []]
    for w in g.get("weeks") or []:
        w["totals"] = strip(w.get("totals") or {})
    for key in ("totals", "period_to_date"):
        if g.get(key):
            g[key] = strip(g[key])
    g["withheld"] = ["budget"] + (["labor"] if "labor" in gone else []) + (["gross"] if "gross" in gone else [])
    return g
