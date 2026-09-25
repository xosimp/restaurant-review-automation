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
    if (k in LOSS_KEYS or k.startswith("loss")) and not _sees(user, LOSS_VIEW):
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
    out["blocks"] = blocks
    out["missing"] = dsr.missing_reasons(blocks)
    out["withheld"] = [n for n in dsr.BLOCKS if n in withheld]
    return out, hidden


def _cites_hidden(cites, hidden):
    for c in cites or ():
        c = str(c)
        for h in hidden:
            if c == h or (h.endswith(".") and c.startswith(h)) or c.startswith(h + ".") or c.startswith(h + ":"):
                return True
    return False


def filter_narrative(narrative, hidden):
    """The narrative with every item that cites a withheld fact removed —
    and, when anything is withheld, every uncited sentence too."""
    if not isinstance(narrative, dict):
        return None
    if not hidden:
        return copy.deepcopy(narrative)

    def keep(node, cited=False):
        if isinstance(node, dict):
            refs = node.get("cites") if isinstance(node.get("cites"), list) else node.get("facts")
            if isinstance(refs, list):
                if _cites_hidden(refs, hidden):
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
    return n


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
            "missing": facts.get("missing") or []}


def render(report, user, restaurant=None, versions=None):
    """The whole report for this login: facts, narrative and checklist, all
    from the one stored snapshot."""
    from time_utils import mdy
    view = view_for(user)
    facts, hidden = redact(report.get("facts") or {}, user)
    # "Did we win today?" — the owner's scorecard (dsr.scorecard): score,
    # wins and risks, all from the measured blocks. The manager's view keeps
    # its own layout: the budget and the food estimate are the owner's.
    card = None
    if view == OWNER:
        try:
            from dsr import scorecard
            card = scorecard.build(report.get("facts") or {}, restaurant, narrative=report.get("narrative"))
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
        "tomorrow": tomorrow_for(facts, user, view),
        "insights": insights(facts, report.get("narrative") and narrative_for(report.get("narrative"), hidden), view),
        "yesterday": _yesterday(report, restaurant),
        "checklist": checklist(report, user, restaurant),
        "versions": [dict(v, finalized_at_local=_stamp_local(v.get("finalized_at"), restaurant),
                          created_at_local=_stamp_local(v.get("created_at"), restaurant))
                     for v in (versions or [])],
    }


def tomorrow_for(facts, user, view):
    """The stored next-night snapshot (dsr.tomorrow) as this login may read
    it: a manager without the Food view loses the stock lines."""
    t = (facts or {}).get("tomorrow")
    if not isinstance(t, dict):
        return None
    t = copy.deepcopy(t)
    t.pop("_preds", None)
    if "food" in ((facts or {}).get("withheld") or []):
        t["items"] = [i for i in t.get("items") or [] if i.get("kind") != "stock"]
    return t


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


def _yesterday(report, restaurant):
    """"How did yesterday turn out?" — what the previous night's report
    predicted about this night, graded (dsr.predictions)."""
    try:
        from dsr import predictions
        rid = report.get("restaurant_id") or getattr(restaurant, "id", None)
        return predictions.review(rid, report.get("business_date")) if rid else None
    except Exception:
        return None


def summary(report, user):
    """One row of the report list — with what Home's "Last night" card
    shows: the night's net sales when the Sales block is ready (None
    otherwise, never 0), the narrative's lead as this login may read it, and
    when there is no lead, the reason the summary wasn't written."""
    from time_utils import mdy
    facts, hidden = redact(report.get("facts") or {}, user)
    sales = (facts.get("blocks") or {}).get("sales") or {}
    net = (sales.get("metrics") or {}).get("net") if sales.get("status") == dsr.READY else None
    narrative = filter_narrative(report.get("narrative"), hidden) or {}
    lead = narrative.get("executive_summary") if isinstance(narrative, dict) else None
    lead_text = lead.get("text") if isinstance(lead, dict) else None
    note = (report.get("stages") or {}).get("narrative") or {}
    return {"business_date": report.get("business_date"), "label": mdy(report.get("business_date")),
            "version": report.get("version"), "status": report.get("status"),
            "provisional": bool(report.get("provisional")), "missing": facts.get("missing") or [],
            "finalized_at": report.get("finalized_at"), "net": net, "lead": lead_text,
            "lead_missing": None if lead_text else (note.get("reason") if isinstance(note, dict) else None)}


def redact_grid(grid, user):
    """A week or period from dsr.rollup, as this login may read it: the
    budget columns (and their comparisons) are the owner's, as on the
    nightly report — and the labor columns go too for a login without
    LABOR_VIEW, as the nightly report drops its Labor block. Returns a copy;
    None passes through."""
    from permissions import LABOR_VIEW
    if grid is None:
        return None
    if view_for(user) == OWNER:
        return grid
    gone = OWNER_ONLY_PREFIXES + (() if _sees(user, LABOR_VIEW) else ("labor",))

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
    g["withheld"] = ["budget"] + (["labor"] if "labor" in gone else [])
    return g
