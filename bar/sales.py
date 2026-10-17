"""
bar/sales.py — what the POS sold to drink, night by night, on one stated
basis: POS drink sales after comps AND check discounts (bar fix round
10/15/26: V5, V6, V7, R10, R23).

Where a night's figures come from (first that applies):

  report      the nightly report's department dollars (`sales.dep:*`) and
              the POS item categories inside them (`sales.depcat:*`), read
              under the bar mapping AS IT STANDS NOW (bar.mapping) — never
              the categories the report froze when it was built
              (`sales.cat:*`). Simple EJ's first report (9/27/26) froze
              $11,617.28 as "Unmapped" before the departments were mapped,
              and the Bar left the night's $4,040 of drinks out while the
              window read complete (FIN-3 / RPW-2).
  archive     the ticket archive's sale lines by department and item
              category (bar_sales_nights, derived from pos_ticket_lines by
              derive_nights) — for a night that never wrote department
              dollars, and for the item-category split of a night whose
              report kept departments only.
  report_departments / report_categories
              older nights with departments only, or the owner's report
              categories only (an imported workbook): mapped at that level.

Check discounts (V6 / RPW-1): RPOWER rings a check discount as its own line
under its own department ("Discounts" at EJ's: −$18,645 in four weeks, about
$8,818 of it on drink tickets), so department dollars are BEFORE them. Each
archived ticket's discount lines are allocated across that ticket's sale
lines (derive_nights): RPOWER's own `discounted` field when the ticket's
lines add up to its discounts exactly, else the line a discount is linked to
(ticketitem's parent_atom), else pro rata by each line's sales. A discount
that takes a whole ticket (or is named "100%") is a comp: the drink was
poured and not paid for, kept apart as `comps_usd` ("poured, not paid")
beside the comp-type lines already outside the department dollars. A night
with no archive estimates its discounts pro rata from its negative
departments, and says so (`estimated_nights`).

A night with net sales but no drink coverage — nothing by department or
item category on file, a report that filed sales as "Unmapped", or only
unmapped categories — is a named sales GAP: left out of the per-day
figures, and returned (window_basis) so the engine never judges a window
around it. Unmapped sales are named, never silently dropped (R10).

Shapes:
  _sales(rid)        {beer|liquor|wine: {date: net}} — what bar.pour_cost
                     has always read (net now; gap nights left out)
  bar_sales(rid)     the whole detail: by_category, gross_by_category,
                     nights {date: {...}}, mapping, basis_all
  window_basis(detail, start, end)
                     the window's `sales_basis` (PLAN payload contract) plus
                     its gaps and coverage
"""
import re
from collections import defaultdict

from bar.common import POUR_CATEGORIES, _mdy, get_conn
from bar.mapping import Mapping, key_for

DRINK_KINDS = ("beer", "liquor", "wine", "na")
UNMAPPED_REPORT = "Unmapped"          # dsr.UNMAPPED: the report's own name for sales it couldn't place
# Unmapped sales above this share of the window's DRINK sales (beer, liquor,
# wine and the unmapped themselves — never the whole restaurant's, where
# food dilutes it: BAR-CALC-03) are said on the page (R10), and hold every
# reading back (bar.judge.UNMAPPED_HOLD_PCT).
UNMAPPED_SAY_PCT = 2.0
_FULL_COMP_RE = re.compile(r"100\s*%")
_UNASSIGNED = "Unassigned"            # rpower.UNASSIGNED_DEPARTMENT


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── each archived night, net of its check discounts (written at archive time) ─

def _filer(conn, restaurant_id):
    """A function line -> (department, item category) for archived lines:
    the line's own (layout 4), else the POS catalog kept beside the archive
    (pos_menu_catalog -> pos_sales_categories) for lines stored before it."""
    item_cat, cats = {}, {}
    try:
        item_cat = {str(r[0]): r[1] for r in conn.execute(
            "SELECT item_id, category_id FROM pos_menu_catalog WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
        cats = {str(r[0]): (r[1], r[2]) for r in conn.execute(
            "SELECT category_id, name, department FROM pos_sales_categories WHERE restaurant_id=?",
            (restaurant_id,)).fetchall()}
    except Exception:
        pass

    def filed(r):
        dep, sub = (r["department"] or "").strip(), (r["sales_category"] or "").strip()
        if not dep:
            cat_id = r["category_id"] or item_cat.get(str(r["item_id"] or ""))
            name, d = cats.get(str(cat_id or ""), (None, None))
            dep = (d or "").strip() or _UNASSIGNED
            sub = sub or (name or "").strip()
        return dep, sub
    return filed


def _allocate(rows, filed) -> tuple:
    """({(department, item category): cell}, basis) for one night's archived
    lines — see the module note. A cell: sales (sale lines), discount_lines
    (its own discount lines, negative), comp_lines (comp-type lines' value),
    discounts (check discounts allocated to it, positive = taken off) and
    comps100 (the part of those that were 100% comps)."""
    tickets = defaultdict(lambda: {"sale": [], "discount": [], "comp": []})
    for r in rows:
        if r["kind"] in ("sale", "discount", "comp"):
            tickets[r["ticket_id"] or ""][r["kind"]].append(r)
    cells = {}

    def cell(r):
        return cells.setdefault(filed(r), {"sales": 0.0, "discount_lines": 0.0, "comp_lines": 0.0,
                                          "discounts": 0.0, "comps100": 0.0})

    used = set()
    for t in tickets.values():
        for s in t["sale"]:
            cell(s)["sales"] += _f(s["sales"])
        for c in t["comp"]:
            cell(c)["comp_lines"] += abs(_f(c["loss_amount"])) or abs(_f(c["sales"]))
        for d in t["discount"]:
            cell(d)["discount_lines"] += _f(d["sales"])
        total = -sum(_f(d["sales"]) for d in t["discount"])        # positive: money taken off
        if abs(total) < 0.005:
            continue
        sold = [s for s in t["sale"] if _f(s["sales"]) > 0]
        ticket_sales = sum(_f(s["sales"]) for s in sold)
        whole = ticket_sales > 0 and total >= ticket_sales - 0.01
        by_id = {s["line_id"]: s for s in sold}
        # RPOWER's own allocation, when its `discounted` on the ticket's sale
        # lines adds up to the ticket's discount lines (its semantics are
        # undocumented, so it is used only where it reconciles).
        own = [abs(_f(s["discounted"])) for s in sold]
        if sold and sum(own) > 0 and abs(sum(own) - abs(total)) <= 0.02:
            used.add("rpower")
            named = all(_FULL_COMP_RE.search(str(d["item_name"] or "")) for d in t["discount"])
            sign = 1.0 if total > 0 else -1.0
            for s, v in zip(sold, own):
                c = cell(s)
                c["discounts"] += sign * v
                if whole or named:
                    c["comps100"] += sign * v
            continue
        used.add("pro_rata")
        for d in t["discount"]:
            amount = -_f(d["sales"])
            full = whole or bool(_FULL_COMP_RE.search(str(d["item_name"] or "")))
            parent = by_id.get(d["parent_line_id"]) if d["parent_line_id"] else None
            if parent is not None:
                targets = [(parent, amount)]
                full = full or amount >= _f(parent["sales"]) - 0.01
            elif ticket_sales > 0:
                targets = [(s, amount * _f(s["sales"]) / ticket_sales) for s in sold]
            else:
                c = cells.setdefault(("", ""), {"sales": 0.0, "discount_lines": 0.0, "comp_lines": 0.0,
                                                "discounts": 0.0, "comps100": 0.0})
                c["discounts"] += amount                            # nothing on the ticket to take it from
                continue
            for s, share in targets:
                c = cell(s)
                c["discounts"] += share
                if full:
                    c["comps100"] += share
    basis = "rpower_discounted" if used == {"rpower"} else ("pro_rata" if used == {"pro_rata"} else
                                                             ("mixed" if used else "no_discounts"))
    return cells, basis


def derive_nights(restaurant_id, dates, db_path=None, provider=None) -> dict:
    """Store each archived night's sales by POS department and item category
    with its check discounts allocated per ticket (bar_sales_nights; the
    night replaced whole). Mapping-independent: a change of bar mapping
    never needs it again. Called by pos_archive.archive_day for every day it
    stores, and by repair_nights for the archive already on file.
    {nights, cells}."""
    rid = int(restaurant_id)
    out = {"nights": 0, "cells": 0}
    dates = sorted({str(d)[:10] for d in dates or [] if d})
    if not dates:
        return out
    conn = get_conn(db_path)
    try:
        filed = _filer(conn, rid)
        for iso in dates:
            q = ("SELECT line_id, ticket_id, item_id, item_name, kind, sales, loss_amount, department, "
                 "sales_category, category_id, discounted, parent_line_id FROM pos_ticket_lines "
                 "WHERE restaurant_id=? AND business_date=?")
            args = [rid, iso]
            if provider:
                q += " AND provider=?"
                args.append(provider)
            rows = conn.execute(q, args).fetchall()
            conn.execute("DELETE FROM bar_sales_nights WHERE restaurant_id=? AND business_date=?", (rid, iso))
            if not rows:
                continue
            cells, basis = _allocate(rows, filed)
            conn.executemany(
                "INSERT INTO bar_sales_nights (restaurant_id, business_date, department, sales_category, sales, "
                "discounts, comps100, comp_lines, discount_lines, basis, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                [(rid, iso, dep, sub, round(c["sales"], 2), round(c["discounts"], 2), round(c["comps100"], 2),
                  round(c["comp_lines"], 2), round(c["discount_lines"], 2), basis)
                 for (dep, sub), c in cells.items()])
            out["nights"] += 1
            out["cells"] += len(cells)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return out


def repair_nights(restaurant_id, db_path=None, since=None) -> dict:
    """derive_nights for every archived night on file (or from `since`) —
    idempotent; what the lead runs once after deploy
    (scripts/bar_sales_repair.py), and safe to run again."""
    conn = get_conn(db_path)
    try:
        q = "SELECT DISTINCT business_date FROM pos_ticket_lines WHERE restaurant_id=?"
        args = [int(restaurant_id)]
        if since:
            q += " AND business_date >= ?"
            args.append(str(since)[:10])
        dates = [str(r[0])[:10] for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()
    return derive_nights(restaurant_id, dates, db_path=db_path)


# ── reading the nights under the mapping as it stands ─────────────────────

def _metrics(conn, rid, since, until):
    """{date: {metric: value}} — sales.net, sales.dep:*, sales.depcat:* and
    sales.cat:*, each read as a metric RANGE on the (restaurant, metric,
    date) index rather than a LIKE over every metric the restaurant has
    (R23)."""
    clauses = ("metric = 'sales.net'", "(metric >= 'sales.cat:' AND metric < 'sales.cat;')",
               "(metric >= 'sales.dep:' AND metric < 'sales.dep;')",
               "(metric >= 'sales.depcat:' AND metric < 'sales.depcat;')")
    out = defaultdict(dict)
    for clause in clauses:
        q = f"SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? AND {clause} AND value IS NOT NULL"
        args = [rid]
        if since:
            q += " AND business_date >= ?"
            args.append(since)
        if until:
            q += " AND business_date <= ?"
            args.append(until)
        for r in conn.execute(q, args).fetchall():
            out[str(r["business_date"])[:10]][str(r["metric"])] = float(r["value"])
    return out


def _archived(conn, rid, since, until):
    """{date: {(department lower, item category lower): row}} from bar_sales_nights."""
    out = defaultdict(dict)
    q = ("SELECT business_date, department, sales_category, sales, discounts, comps100, comp_lines, "
         "discount_lines, basis FROM bar_sales_nights WHERE restaurant_id=?")
    args = [rid]
    if since:
        q += " AND business_date >= ?"
        args.append(since)
    if until:
        q += " AND business_date <= ?"
        args.append(until)
    try:
        rows = conn.execute(q, args).fetchall()
    except Exception:
        return out
    for r in rows:
        out[str(r["business_date"])[:10]][(str(r["department"] or "").strip().lower(),
                                           str(r["sales_category"] or "").strip().lower())] = dict(r)
    return out


def _split(name):
    from bar.mapping import split_key
    return split_key(name)


def _night(m, met, arch):
    """One night: its cells under the mapping, its drink figures and its gap."""
    net = met.get("sales.net")
    deps = {k[len("sales.dep:"):]: v for k, v in met.items() if k.startswith("sales.dep:")}
    subs = {}
    for k, v in met.items():
        if k.startswith("sales.depcat:"):
            dep, sub = _split(k[len("sales.depcat:"):])
            if sub:
                subs[(dep, sub)] = v
    cats = {k[len("sales.cat:"):]: v for k, v in met.items() if k.startswith("sales.cat:")}
    raw = []                                         # (key_kind, key, department, item category, gross)
    if deps and subs:
        source = "report"
        inside = defaultdict(float)
        for (dep, sub), v in subs.items():
            raw.append(key_for(dep, sub) + (dep, sub, v))
            inside[dep.lower()] += v
        for dep, v in deps.items():
            rest = round(v - inside.get(dep.lower(), 0.0), 2)
            if abs(rest) >= 0.01 or dep.lower() not in inside:
                raw.append(key_for(dep) + (dep, None, rest))
    elif arch:
        source = "archive" if not deps else "report_with_archive_split"
        for (dep_l, sub_l), a in arch.items():
            dep, sub = a["department"], (a["sales_category"] or None)
            if not dep and not sub:
                continue                             # the night's unallocated discounts: not a sale
            raw.append(key_for(dep, sub) + (dep, sub, _f(a["sales"]) + _f(a["discount_lines"])))
    elif deps:
        source = "report_departments"
        for dep, v in deps.items():
            raw.append(key_for(dep) + (dep, None, v))
    elif cats:
        source = "report_categories"
        for cat, v in cats.items():
            if cat != UNMAPPED_REPORT:
                raw.append(("report_category", cat, None, None, v))
    else:
        source = None
    cells, drinks = [], {k: {"gross": 0.0, "discounts": 0.0, "comps100": 0.0, "comp_lines": 0.0, "own": 0.0,
                             "cells": 0} for k in DRINK_KINDS}
    unmapped = 0.0
    positive = negative_other = 0.0
    for key_kind, key, dep, sub, gross in raw:
        res = m.resolve(key_kind, key)
        a = (arch or {}).get(((dep or "").lower(), (sub or "").lower())) if dep is not None else None
        c = {"key_kind": key_kind, "key": key, "department": dep, "item_category": sub, "gross": round(gross, 2),
             "kind": res["kind"], "confirmed": res["confirmed"], "source": res["source"], "why": res["why"],
             "discounts": round(_f(a["discounts"]), 2) if a else 0.0,
             "comps100": round(_f(a["comps100"]), 2) if a else 0.0,
             "comp_lines": round(_f(a["comp_lines"]), 2) if a else 0.0,
             "own_discounts": round(_f(a["discount_lines"]), 2) if a else 0.0}
        cells.append(c)
        if gross > 0:
            positive += gross
        if res["kind"] in DRINK_KINDS:
            d = drinks[res["kind"]]
            d["cells"] += 1
            d["gross"] += gross
            d["discounts"] += c["discounts"]
            d["comps100"] += c["comps100"]
            d["comp_lines"] += c["comp_lines"]
            d["own"] += c["own_discounts"]
        elif res["kind"] is None:
            if gross > 0:
                unmapped += gross
        elif gross < 0:
            negative_other += -gross
    discount_basis = "archive" if arch else None
    drink_gross = sum(d["gross"] for d in drinks.values())
    if not arch and negative_other > 0 and positive > 0 and drink_gross > 0:
        # No ticket archive for the night: its negative departments (check
        # discounts) are spread over its sales pro rata — an estimate, said.
        discount_basis = "estimated"
        for d in drinks.values():
            d["discounts"] = negative_other * d["gross"] / positive
    for d in drinks.values():
        # Department dollars already hold a drink category's own discount
        # lines; the allocation takes every discount once (module note).
        d["net"] = round(d["gross"] - d["own"] - d["discounts"], 2)
        d["discounts_only"] = round(d["discounts"] - d["comps100"], 2)
        for k in ("gross", "discounts", "comps100", "comp_lines", "own"):
            d[k] = round(d[k], 2)
    gap = None
    if net is not None and net > 0:
        if source == "report_categories" and _f(cats.get(UNMAPPED_REPORT)) > 0.005:
            gap = {"why": "unmapped_in_report",
                   "say": f"the night's report filed ${_f(cats.get(UNMAPPED_REPORT)):,.0f} of sales as unmapped"}
        elif not raw:
            gap = {"why": "no_departments", "say": "no sales by department or item category on file"}
        elif drink_gross <= 0 and unmapped > 0:
            gap = {"why": "no_drinks_mapped", "say": "none of the night's sales map to a drink category yet"}
    return {"net": net, "source": source, "discount_basis": discount_basis, "gap": gap, "cells": cells,
            "drinks": drinks, "unmapped": round(unmapped, 2)}


def bar_sales(restaurant_id, db_path=None, since=None, until=None, mapping=None) -> dict:
    """Every night's drink sales under the mapping as it stands (module
    note): {by_category {beer|liquor|wine|na: {date: net}}, gross_by_category,
    nights {date: night}, mapping (bar.mapping.Mapping), basis_all}."""
    rid = int(restaurant_id)
    since = str(since)[:10] if since else None
    until = str(until)[:10] if until else None
    conn = get_conn(db_path)
    try:
        met = _metrics(conn, rid, since, until)
        arch = _archived(conn, rid, since, until)
    finally:
        conn.close()
    m = mapping or Mapping(rid, db_path)
    nights, by_cat, gross_by_cat = {}, defaultdict(dict), defaultdict(dict)
    for d in sorted(set(met) | set(arch)):
        n = _night(m, met.get(d, {}), arch.get(d))
        nights[d] = n
        if n["gap"]:
            continue
        for k, v in n["drinks"].items():
            if v["cells"]:
                by_cat[k][d] = v["net"]
                gross_by_cat[k][d] = v["gross"]
    detail = {"by_category": dict(by_cat), "gross_by_category": dict(gross_by_cat), "nights": nights, "mapping": m}
    detail["basis_all"] = window_basis(detail, None, None)
    return detail


def window_basis(detail, start=None, end=None, expected=None) -> dict:
    """The sales basis of [start, end] (PLAN payload `sales_basis`): {label,
    discounts_usd, comps_usd, unmapped_usd, unmapped_pct (of the drink
    sales), unmapped_say, unmapped_hold, mapping_needed, unmapped_categories
    [{key, kind, sales_usd, why}], gross_usd, net_usd, nights,
    covered_nights, estimated_nights, gaps [{date, date_mdy, why, say,
    net}]}. Drinks here are beer, liquor and wine — the pour categories;
    non-alcoholic drinks are `na_usd`. `expected` (ISO dates the bar
    trades, bar.counts._trading_days) names a night with no report at all
    as a gap too (`no_report`, BAR-CALC-02) — before, a night that never
    arrived appeared nowhere."""
    from bar.judge import UNMAPPED_HOLD_PCT
    dis = comps = poured = unmapped = gross = net = na = 0.0
    needed = False
    unm = defaultdict(lambda: {"sales_usd": 0.0, "why": None, "kind": None})
    gaps, covered, estimated, count = [], 0, 0, 0
    for d, n in sorted((detail.get("nights") or {}).items()):
        if (start and d < start) or (end and d > end):
            continue
        count += 1
        if n["gap"]:
            gaps.append({"date": d, "date_mdy": _mdy(d), "why": n["gap"]["why"], "say": n["gap"]["say"],
                         "net": n["net"]})
            continue
        covered += 1
        estimated += 1 if n["discount_basis"] == "estimated" else 0
        for k in POUR_CATEGORIES:
            v = n["drinks"][k]
            dis += v["discounts_only"]
            comps += v["comps100"] + v["comp_lines"]
            poured += v["comps100"] + v["comp_lines"]
            gross += v["gross"]
            net += v["net"]
        na += n["drinks"]["na"]["net"]
        for c in n["cells"]:
            if c["gross"] > 0 and not c["confirmed"]:
                needed = True
            if c["kind"] is None and c["gross"] > 0:
                u = unm[(c["key_kind"], c["key"])]
                u["sales_usd"] += c["gross"]
                u["why"], u["kind"] = c["why"], c["key_kind"]
                unmapped += c["gross"]
    nights = detail.get("nights") or {}
    for d in expected or ():
        if (start and d < start) or (end and d > end) or d in nights:
            continue
        gaps.append({"date": d, "date_mdy": _mdy(d), "why": "no_report",
                     "say": "no nightly report on file for a night the bar trades", "net": None})
    gaps.sort(key=lambda g: g["date"])
    # Against the drink sales, not the restaurant's (BAR-CALC-03).
    base = gross + unmapped
    pct = round(unmapped / base * 100, 1) if base > 0 and unmapped > 0 else 0.0
    label = "POS drink sales after comps and check discounts"
    if estimated:
        label += f" (check discounts estimated for {estimated} night{'s' if estimated != 1 else ''})"
    return {"label": label, "discounts_usd": round(dis, 2), "comps_usd": round(comps, 2),
            "comps_label": "Poured, not paid", "poured_not_paid_usd": round(poured, 2),
            "unmapped_usd": round(unmapped, 2), "unmapped_pct": pct, "unmapped_base": "drink_sales",
            "unmapped_say": pct > UNMAPPED_SAY_PCT, "unmapped_hold": pct > UNMAPPED_HOLD_PCT,
            "mapping_needed": needed,
            "unmapped_categories": sorted(({"key": k[1], "kind": v["kind"], "sales_usd": round(v["sales_usd"], 2),
                                            "why": v["why"]} for k, v in unm.items()),
                                          key=lambda x: -x["sales_usd"]),
            "gross_usd": round(gross, 2), "net_usd": round(net, 2), "na_usd": round(na, 2),
            "nights": count, "covered_nights": covered, "estimated_nights": estimated, "gaps": gaps}


def _sales(restaurant_id, db_path=None) -> dict:
    """{beer|liquor|wine: {date: sales}} — POS drink sales after comps and
    check discounts, each night under the bar mapping as it stands; a night
    with a sales gap is left out (window_basis names it). The shape
    bar.pour_cost has always read."""
    by = bar_sales(restaurant_id, db_path=db_path)["by_category"]
    return {k: dict(by.get(k) or {}) for k in POUR_CATEGORIES if by.get(k)}
