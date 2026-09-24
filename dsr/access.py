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
be safe. `meta` passes through untouched.
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
        "narrative": filter_narrative(report.get("narrative"), hidden),
        "checklist": checklist(report, user, restaurant),
        "versions": [dict(v, finalized_at_local=_stamp_local(v.get("finalized_at"), restaurant),
                          created_at_local=_stamp_local(v.get("created_at"), restaurant))
                     for v in (versions or [])],
    }


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
    nightly report. Returns a copy; None passes through."""
    if grid is None:
        return None
    if view_for(user) == OWNER:
        return grid
    import copy

    def strip(d):
        return {k: v for k, v in d.items() if not str(k).startswith(OWNER_ONLY_PREFIXES)}

    g = copy.deepcopy(grid)
    for key in ("days",):
        g[key] = [strip(r) for r in g.get(key) or []]
    for w in g.get("weeks") or []:
        w["totals"] = strip(w.get("totals") or {})
    for key in ("totals", "period_to_date"):
        if g.get(key):
            g[key] = strip(g[key])
    g["withheld"] = ["budget"]
    return g
