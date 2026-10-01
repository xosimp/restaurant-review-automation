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

# "timeclock": punches a manager edited (dsr.block_service) — the owner's
# check on the people who edit them (9/30/26).
OWNER_ONLY_PREFIXES = ("budget", "vs_budget", "prime_cost", "source_checks", "salaried", "timeclock")
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
BLOCK_LABELS = {"sales": "Sales", "labor": "Labor", "service": "Service", "food": "Food", "reviews": "Reviews",
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
    "salaries": r"salar\w*",
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
    if any(h.split(".", 1)[-1].startswith("salaried") for h in hs):
        words.append(_TOPIC_WORDS["salaries"])
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
    stored = _live_weather(_live_target(_live_recost(_live_labor(_live_categories(report.get("facts") or {}, report, restaurant),
                                                                 report, restaurant), report), restaurant), report)
    if view == OWNER:
        stored = _live_salaries(_live_budget(stored, report, restaurant), restaurant)
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
        _kpis = None
        kp = {"top": [], "operations": [], "shift": None}
    shown = narrative_for(report.get("narrative"), hidden)
    # The few KPIs the report shows up front (ID1-16, 9/25/26): the owner's
    # skip the four Today's score already states; the rest sit under "All
    # KPIs". Keys into `kpis`, so every surface draws the same few.
    try:
        headline = _kpis.headline(kp.get("top") or [], card) if _kpis else []
    except Exception:
        headline = []
    tmr = tomorrow_for(stored, user, view, withheld=facts.get("withheld"))
    # The week to date and the six KPIs the report leads with (owner,
    # 9/30/26: large, animated, scannable). Read at render so a budget
    # entered tomorrow morning moves this week's pace.
    try:
        pace = _kpis.pace(facts, restaurant, view) if _kpis else None
    except Exception:
        pace = None
    try:
        big = _kpis.big(kp.get("top") or [], kp.get("operations_all") or kp.get("operations") or [], pace, tmr,
                       view) if _kpis else []
    except Exception:
        big = []
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
        "narrative": shown,
        "scorecard": card,
        "kpis": kp.get("top") or [],
        "kpis_headline": headline,
        "operations": kp.get("operations") or [],
        "shift": kp.get("shift"),
        "tomorrow": tmr,
        "pace": pace,
        "kpis_big": big,
        "insights": insights(facts, shown, view, said=_said(card, shown)),
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
    # Tomorrow's labor, the week's overtime and a 7th day in a row are the
    # Labor view's; the salaried share is the owner's (OWNER_ONLY_PREFIXES).
    if "labor" in (withheld or []):
        t.pop("labor", None)
        t.pop("overtime", None)
        t["items"] = [i for i in t.get("items") or [] if i.get("kind") != "rest_day"]
    elif isinstance(t.get("labor"), dict) and view != OWNER:
        t["labor"] = {k: v for k, v in t["labor"].items() if not str(k).lower().startswith(OWNER_ONLY_PREFIXES)}
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


# The callouts the report already says elsewhere (ID1-18, 9/25/26): the
# biggest win and risk are Today's wins and risks, the top priority is
# Tomorrow's priority #1. What is left — staffing, guests, the biggest
# opportunity — is kept, at most INSIGHTS_MAX, and never a sentence already
# on the page word for word.
RESTATED_INSIGHTS = ("biggest_win", "biggest_risk", "highest_priority_issue")
INSIGHTS_MAX = 2
INSIGHT_LABELS = (("biggest_staffing_concern", "Staffing"), ("largest_staffing", "Staffing"),
                  ("largest_guest_experience", "Guests"),
                  ("biggest_financial_opportunity", "Biggest opportunity"),
                  ("largest_opportunity", "Biggest opportunity"))


def _norm(text):
    return " ".join(str(text).lower().split())


def insights(facts, narrative, view, said=()):
    """AI insights: the narrative's verified callouts the report does not
    already say — staffing, guests, the biggest opportunity — at most
    INSIGHTS_MAX. `said` is the text already on the page (wins, risks,
    went well / needs attention, the actions); an insight repeating one word
    for word is left out.

    The Food block's money at stake is not an insight (ID1-16): the Food
    block's own "At stake · opportunity" tile carries it, labelled an
    opportunity, once. `facts` and `view` stay in the signature for its
    callers; the narrative passed in is already this view's (narrative_for),
    so nothing here can reach a line the view withholds."""
    out = []
    n = narrative if isinstance(narrative, dict) else {}
    seen = {_norm(t) for t in said or () if t}
    for key, label in INSIGHT_LABELS:
        if len(out) >= INSIGHTS_MAX:
            break
        item = n.get(key)
        text = item.get("text") if isinstance(item, dict) else None
        if text and _norm(text) not in seen:
            seen.add(_norm(text))
            out.append({"kind": key, "label": label, "text": text, "source": "narrative"})
    return out


def _said(card, narrative):
    """The sentences the report already carries around its insights: Today's
    wins and risks, went well / needs attention, and Tomorrow's priorities."""
    out = []
    for x in ((card or {}).get("wins") or []) + ((card or {}).get("risks") or []):
        if isinstance(x, dict) and x.get("text"):
            out.append(x["text"])
    n = narrative if isinstance(narrative, dict) else {}
    for key in ("went_well", "needs_attention", "actions_tomorrow"):
        for x in n.get(key) or []:
            t = x.get("text") if isinstance(x, dict) else x
            if isinstance(t, str) and t:
                out.append(t)
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


def _live_categories(facts, report, restaurant):
    """The facts with the Sales block's categories under the category map as
    it stands NOW (block_sales.live_categories): the owner maps departments
    after the first report, and a built report kept saying "unmapped"
    (Simple EJ's, 9/28/26). The dollars are the night's; only which
    category each department counts toward is read today. Every view."""
    sales = ((facts or {}).get("blocks") or {}).get("sales") or {}
    if sales.get("status") != dsr.READY or not sales.get("detail"):
        return facts
    try:
        from dsr.block_sales import live_categories
        rid = report.get("restaurant_id") or getattr(restaurant, "id", None)
        net = (sales.get("metrics") or {}).get("net")
        cats, unmapped = live_categories(sales.get("detail") or {}, rid, net)
    except Exception:
        return facts
    out = copy.deepcopy(facts)
    s = out["blocks"]["sales"]
    d = s.setdefault("detail", {})
    d["categories"], d["unmapped"] = cats, unmapped
    m = s.setdefault("metrics", {})
    for k in [k for k in m if k.startswith("cat:")]:
        del m[k]
    for c in cats:
        m[f"cat:{c['category']}"] = c["net"]
    if unmapped:
        m[f"cat:{dsr.UNMAPPED}"] = round(sum(float(u.get("net") or 0) for u in unmapped), 2)
    return out


RECOST_MIN_DOLLARS = 1.0


def _live_recost(facts, report):
    """A night's hourly labor as the daily history holds it now, where pay
    changed after the report (models.recost_labor_history — Simple EJ's,
    9/30/26: three managers made salaried, their punches leave hourly labor
    and their salaries are added by _live_salaries). The report's own figure
    is kept as detail.recosted_from; detail.recosted_after_report says so.
    A report that withheld its dollars is _live_labor's."""
    labor = ((facts or {}).get("blocks") or {}).get("labor") or {}
    lm = labor.get("metrics") or {}
    if labor.get("status") != dsr.READY or lm.get("cost") is None:
        return facts
    try:
        from dsr import store
        rid = report.get("restaurant_id")
        day = str(report.get("business_date"))[:10]
        conn = store.get_conn()
        try:
            h = conn.execute("SELECT labor_cost FROM labor_daily_history WHERE restaurant_id=? AND date=? "
                             "AND COALESCE(final, 1)=1", (rid, day)).fetchone()
        finally:
            conn.close()
        cost = float(h["labor_cost"]) if h and h["labor_cost"] is not None else None
    except Exception:
        return facts
    if cost is None or abs(cost - float(lm["cost"])) < RECOST_MIN_DOLLARS:
        return facts
    out = copy.deepcopy(facts)
    lb = out["blocks"]["labor"]
    m = lb.setdefault("metrics", {})
    net = ((((out.get("blocks") or {}).get("sales") or {}).get("metrics")) or {}).get("net")
    lb.setdefault("detail", {}).update({"recosted_after_report": True, "recosted_from": m.get("cost")})
    m["cost"] = round(cost, 2)
    if net:
        m["pct"] = round(cost / float(net) * 100.0, 1)
        if m.get("target_pct") is not None:
            m["vs_target_pts"] = round(m["pct"] - float(m["target_pct"]), 1)
    return out


def _live_labor(facts, report, restaurant):
    """A Labor block that withheld its dollars for want of a wage rate, costed
    now where the POS's own pay prices the night (block_labor
    POS_PAY_MIN_SHARE): Simple EJ's 9/27 report was built before RPOWER's
    pay reached the shifts, and said "-" for labor $ and % (owner, 9/28/26).
    The hours are the report's; the dollars are the archive's, costed at the
    POS's wages, and detail.costed_after_report says so."""
    labor = ((facts or {}).get("blocks") or {}).get("labor") or {}
    lm = labor.get("metrics") or {}
    if labor.get("status") != dsr.READY or lm.get("cost") is not None \
            or (labor.get("detail") or {}).get("cost_basis") != "default":
        return facts
    try:
        from dsr import store
        from dsr.block_labor import POS_PAY_MIN_SHARE, _pos_pay_share
        from labor import load_shifts
        from models import get_client_data
        rid = report.get("restaurant_id") or getattr(restaurant, "id", None)
        day = str(report.get("business_date"))[:10]
        raw = (get_client_data(rid) or {}).get("shifts_csv") or ""
        rows = [r for r in (load_shifts(csv_string=raw) if raw.strip() else []) if str(r.get("date") or "")[:10] == day]
        share = _pos_pay_share(rows)
        if share is None or share < POS_PAY_MIN_SHARE:
            return facts
        conn = store.get_conn()
        try:
            h = conn.execute("SELECT labor_cost FROM labor_daily_history WHERE restaurant_id=? AND date=? "
                             "AND COALESCE(final, 1)=1", (rid, day)).fetchone()
        finally:
            conn.close()
        cost = float(h["labor_cost"]) if h and h["labor_cost"] else None
    except Exception:
        return facts
    if not cost:
        return facts
    out = copy.deepcopy(facts)
    lb = out["blocks"]["labor"]
    m = lb.setdefault("metrics", {})
    net = ((((out.get("blocks") or {}).get("sales") or {}).get("metrics")) or {}).get("net")
    m["cost"] = round(cost, 2)
    if net:
        m["pct"] = round(cost / float(net) * 100.0, 1)
        if m.get("target_pct") is not None:
            m["vs_target_pts"] = round(m["pct"] - float(m["target_pct"]), 1)
    d = lb.setdefault("detail", {})
    d.update({"cost_basis": "pos_wages", "costed_after_report": True, "pos_pay_share_pct": round(share * 100),
              "cost_note": ("Costed after this report at the POS's own pay rates"
                            + (f"; {round((1 - share) * 100)}% of the night's hours have no POS rate and use "
                               f"your role or blended rate." if share < 0.995 else "."))})
    return out


def _live_target(facts, restaurant):
    """The Labor block measured against the labor target as it stands NOW:
    Simple EJ's 9/27 report froze "Cavnar AI's starting target 30%" at
    11:31am and Erik set 35% that afternoon (owner, 9/28/26). The figure is
    the night's; the target is the owner's current one (thresholds.target_for,
    the one read every surface uses)."""
    labor = ((facts or {}).get("blocks") or {}).get("labor") or {}
    if labor.get("status") != dsr.READY or restaurant is None:
        return facts
    try:
        import thresholds
        t = thresholds.target_for(restaurant, "labor")
        now = float(t.get("pct"))
    except Exception:
        return facts
    m = labor.get("metrics") or {}
    was = m.get("target_pct")
    d = labor.get("detail") or {}
    if was is not None and abs(float(was) - now) < 1e-9 and d.get("target_source") == t.get("source"):
        return facts
    out = copy.deepcopy(facts)
    lb = out["blocks"]["labor"]
    lm = lb.setdefault("metrics", {})
    lm["target_pct"] = now
    if lm.get("pct") is not None:
        lm["vs_target_pts"] = round(float(lm["pct"]) - now, 1)
    ld = lb.setdefault("detail", {})
    ld["target_source"], ld["target_label"] = t.get("source"), t.get("label")
    return out


def _live_salaries(facts, restaurant):
    """The owner's Labor block with the night's share of the salaries in it:
    salaried_cost is one trading day's share (models.salaried_day_share),
    and pct / cost / vs_target_pts become the all-in figures (owner,
    9/30/26), the shifts alone kept as hourly_pct / hourly_cost. Owner-only
    by where it is called; a manager's report stays hourly."""
    blocks = (facts or {}).get("blocks") or {}
    labor, sales = blocks.get("labor") or {}, blocks.get("sales") or {}
    lm, sm = labor.get("metrics") or {}, sales.get("metrics") or {}
    if restaurant is None or labor.get("status") != dsr.READY or sales.get("status") != dsr.READY:
        return facts
    try:
        from models import salaried_staff, salaried_day_share
        share = salaried_day_share(restaurant)
        people = len(salaried_staff(restaurant))
        cost, net = lm.get("cost"), sm.get("net")
        if not share or cost is None or not net or float(net) <= 0:
            return facts
        total = float(cost) + share         # today's salaries: a changed one recosts the night
    except Exception:
        return facts
    out = copy.deepcopy(facts)
    m = out["blocks"]["labor"].setdefault("metrics", {})
    m["salaried_cost"] = round(share, 2)
    m["salaried_people"] = people
    m["salaried_total_cost"] = round(total, 2)
    m["salaried_total_pct"] = round(total / float(net) * 100.0, 1)
    # The owner's labor % is ALL-IN and the target judges it (owner,
    # 9/30/26: "Erik only cares about that labor %, it's the real %"):
    # pct, cost and vs_target_pts become the salaries-in figures; the shifts
    # alone move to hourly_pct / hourly_cost.
    m["hourly_pct"], m["hourly_cost"] = m.get("pct"), cost
    m["pct"], m["cost"] = m["salaried_total_pct"], m["salaried_total_cost"]
    if m.get("target_pct") is not None:
        try:
            m["vs_target_pts"] = round(float(m["pct"]) - float(m["target_pct"]), 1)
            m["salaried_vs_target_pts"] = m["vs_target_pts"]
        except (TypeError, ValueError):
            pass
    m["includes_salaries"] = True
    return out


def _live_weather(facts, report):
    """The night's OBSERVED weather beside the forecast the Intel block
    stored (memory audit 9/29/26, event_memory): the report is written at
    close, and the nightly event_memory job keeps what the nearest National
    Weather Service station observed that day (weather_daily). Where it is on
    file, detail.weather.observed carries it — Erik's Weather column is the
    weather that happened — and the forecast stays labelled a forecast.
    Nothing else changes; a night with no observation is as stored."""
    intel = ((facts or {}).get("blocks") or {}).get("intel") or {}
    if intel.get("status") != dsr.READY:
        return facts
    try:
        import event_memory
        rid = report.get("restaurant_id")
        obs = event_memory.observed_weather(rid, str(report.get("business_date"))[:10])
    except Exception:
        obs = None
    if not obs:
        return facts
    out = copy.deepcopy(facts)
    d = out["blocks"]["intel"].setdefault("detail", {})
    w = d.get("weather") if isinstance(d.get("weather"), dict) else {}
    bits = [str(obs.get("conditions") or "").strip()]
    if obs.get("high_f") is not None:
        bits.append(f"high {int(round(obs['high_f']))}°")
    if obs.get("rain") == 1:
        bits.append("rain during service")
    w = dict(w, observed={"high_f": obs.get("high_f"), "low_f": obs.get("low_f"), "precip_in": obs.get("precip_in"),
                          "rained_in_service": obs.get("rain"), "conditions": obs.get("conditions"),
                          "station": obs.get("station"), "summary": " · ".join(b for b in bits if b) or None,
                          "basis": "observed by the nearest National Weather Service station"})
    d["weather"] = w
    return out


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


def summary(report, user, restaurant=None):
    """One row of the report list — with what Home's "Last night" card
    shows: the night's net sales when the Sales block is ready (None
    otherwise, never 0), the narrative's lead as this login may read it, and
    when there is no lead, the reason the summary wasn't written.

    The 3-second status (density fix #1, the shared contract Home reads on
    web and iPhone) — every key None-safe:
      verdict     the scorecard's label ("Good day"), owner view only
      tone        "good" | "warn" | "bad" | None, the verdict's
      overall     the score 0–100, or None
      vs_budget   net minus budget in dollars — only where this view may
                  read the budget (redact() drops it for a manager)
      first_risk  the first risk as this view may read it: the owner's
                  scorecard risk (not the budget line the status already
                  carries, as the push does), a manager's first
                  needs-attention line that survived the cite filter

    The scorecard is the one render() builds — the same stored facts with
    the live budget, the same restaurant — so it needs `restaurant`; without
    one the four score keys are None rather than a second reading of the
    night."""
    from time_utils import mdy
    view = view_for(user)
    stored = report.get("facts") or {}
    if view == OWNER and restaurant is not None:
        stored = _live_budget(stored, report, restaurant)
        try:
            stored = _live_salaries(stored, restaurant)
        except Exception:
            pass
    facts, hidden = redact(stored, user)
    sales = (facts.get("blocks") or {}).get("sales") or {}
    ready = sales.get("status") == dsr.READY
    metrics = sales.get("metrics") or {}
    net = metrics.get("net") if ready else None
    vs_budget = metrics.get("vs_budget_net") if ready else None
    if not isinstance(vs_budget, (int, float)) or isinstance(vs_budget, bool):
        vs_budget = None
    # Net against the night before, as the report's Sales block has it — so
    # the phone's Home card draws from this row alone instead of peeking at
    # the whole report as a second call (parity audit #9).
    vs_yesterday = metrics.get("vs_yesterday_pct") if ready else None
    if not isinstance(vs_yesterday, (int, float)) or isinstance(vs_yesterday, bool):
        vs_yesterday = None
    # The same lead the report view opens with (D2-4): narrative_for, so a
    # manager's list row and Home card lead with the operations summary
    # exactly as their report does.
    narrative = narrative_for(report.get("narrative"), hidden) or {}
    lead = narrative.get("executive_summary") if isinstance(narrative, dict) else None
    lead_text = lead.get("text") if isinstance(lead, dict) else None
    note = (report.get("stages") or {}).get("narrative") or {}
    verdict = tone = overall = first_risk = None
    if view == OWNER and restaurant is not None:
        try:
            from dsr import scorecard
            card = scorecard.build(stored, restaurant, narrative=report.get("narrative"))
        except Exception:
            card = None
        if card and card.get("verdict") and card.get("overall") is not None:
            verdict, tone = card["verdict"].get("label"), card["verdict"].get("tone")
            overall = int(card["overall"])
        if card:
            risk = next((x for x in card.get("risks") or [] if x.get("key") != "sales_budget" and x.get("text")), None)
            first_risk = risk["text"] if risk else None
    elif view == MANAGER and isinstance(narrative, dict):
        for item in narrative.get("needs_attention") or []:
            text = item if isinstance(item, str) else (item.get("text") if isinstance(item, dict) else None)
            if text:
                first_risk = str(text).strip()
                break
    return {"business_date": report.get("business_date"), "label": mdy(report.get("business_date")),
            "version": report.get("version"), "status": report.get("status"),
            "provisional": bool(report.get("provisional")), "missing": facts.get("missing") or [],
            "finalized_at": report.get("finalized_at"), "net": net, "lead": lead_text,
            "lead_missing": None if lead_text else lead_missing(report.get("narrative"), note),
            "verdict": verdict, "tone": tone if tone in ("good", "warn", "bad") else None,
            "overall": overall, "vs_budget": round(float(vs_budget), 2) if vs_budget is not None else None,
            "vs_yesterday_pct": round(float(vs_yesterday), 1) if vs_yesterday is not None else None,
            "first_risk": first_risk, "stats": list_stats(facts)}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def list_stats(facts) -> list:
    """The night in a few figures for the Reports list (owner, 9/30/26:
    "instead of long paragraphs, show quick and informative stats"): each
    {key, value, label, tone} from a block that is READY in this view, in a
    fixed order, a figure the night lacks left out - never shown as 0.
    Labor is the view's own (all-in for the owner, hourly for a manager)."""
    blocks = facts.get("blocks") or {}
    out = []

    def ready(name):
        b = blocks.get(name) or {}
        return (b.get("metrics") or {}) if b.get("status") == dsr.READY else {}
    s, lab = ready("sales"), ready("labor")
    for key, label in (("vs_last_week_pct", "vs last week"), ("vs_forecast_pct", "vs forecast")):
        v = _num(s.get(key))
        if v is not None:
            out.append({"key": key, "value": f"{v:+.0f}%", "label": label,
                        "tone": "good" if v >= 0 else "bad"})
    pct, tgt = _num(lab.get("pct")), _num(lab.get("target_pct"))
    if pct is not None:
        out.append({"key": "labor_pct", "value": f"{pct:.1f}%", "label": "labor",
                    "tone": None if tgt is None else ("good" if pct <= tgt else "bad")})
    g, t = _num(s.get("guests")), _num(s.get("avg_ticket"))
    if g:
        out.append({"key": "guests", "value": f"{g:,.0f}", "label": "guests", "tone": None})
    if t:
        out.append({"key": "avg_ticket", "value": f"${t:,.2f}", "label": "avg check", "tone": None})
    ot = _num(lab.get("overtime_hours"))
    if ot:
        out.append({"key": "overtime_hours", "value": f"{ot:,.1f}h", "label": "overtime", "tone": "warn"})
    return out


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


def grid_for(grid, user):
    """A week or period as the app shows it to this login: redact_grid, then
    the one sentence over it (dsr.rollup.story, ID1-22) — written from the
    REDACTED grid, so a view without the budget never reads a budget miss."""
    from dsr import rollup
    g = redact_grid(grid, user)
    if isinstance(g, dict):
        try:
            g["story"] = rollup.story(g)
        except Exception:
            g["story"] = None
    return g
