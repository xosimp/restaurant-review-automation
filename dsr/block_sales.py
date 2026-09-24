"""
dsr.block_sales — the Sales block: `collect(ctx) -> block`.

The night's money from the POS (pos.fetch_day_sales, whose docstring is the
definition of gross and net), set against every baseline the restaurant
actually has:

  yesterday       the night before          ┐ dsr_metrics "sales.net" (the
  last_week       the same weekday, 7 back  │ DSR's own figure), else the
  last_year       the same weekday, 364 back┘ POS sync's labor_daily_history
  budget          dsr_budgets, gross and net as the owner entered them
  forecast        demand.forecast_day — the codebase's one demand forecast
                  (the median of the last eight same weekdays), used only
                  where it has enough weeks to exist

Every comparison is None when its baseline is missing — never 0. A baseline
of zero is a real night with no sales: its dollar difference is shown, its
percentage is not (there is no percentage of nothing).

The categories are Erik's six (dsr.DEFAULT_CATEGORIES) through the owner's
own dsr_category_map; a POS department nobody has mapped is listed as
unmapped with its dollars, never guessed into a category.

Status:
  not_connected  no POS
  unavailable    the POS cannot report a day this way, or rejected the login
  awaiting       the POS has not closed the day, the pull failed, or the POS
                 closed the day with no tickets in it yet
  ready          the figures
"""
import logging
from datetime import timedelta

import dsr
from dsr import store

log = logging.getLogger("dsr")

TOP_N = 5
LAST_YEAR_DAYS = 364      # the same weekday a year back
EVENING_HOUR = 18         # "after 6pm", shared with the Labor block

REASON_NO_POS = "No POS connected"
REASON_CANT_REPORT = "Your POS doesn't report daily sales through Cavnar yet"
REASON_AUTH = "The POS connection needs attention"
REASON_PULL_FAILED = dsr.MISSING_TEXT["sales"]


def _pct(value, base):
    if value is None or base in (None, 0):
        return None
    return round((value / base - 1.0) * 100.0, 1)


def _delta(value, base):
    if value is None or base is None:
        return None
    return round(value - base, 2)


def _baseline_net(ctx, day):
    """(net, source) for another night, or (None, None). The DSR's own
    figure first; before DSRs existed, the nightly POS sync's daily sales
    (labor_daily_history, where a day with no sales synced is 0 and means
    unknown, so only a positive figure counts)."""
    iso = day.isoformat()
    for d, v in store.metric_series(ctx.restaurant_id, "sales.net", iso, iso, db_path=ctx.db_path):
        return float(v), "dsr"
    conn = store.get_conn(ctx.db_path)
    try:
        row = conn.execute("SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date=? "
                           "AND sales IS NOT NULL AND sales > 0", (ctx.restaurant_id, iso)).fetchone()
    finally:
        conn.close()
    return (float(row["sales"]), "pos_sync") if row else (None, None)


def _forecast(ctx):
    import demand
    try:
        fc = demand.forecast_day(ctx.restaurant_id, ctx.business_date, db_path=ctx.db_path)
    except Exception as e:
        log.warning("dsr sales: forecast unavailable rid=%s: %s", ctx.restaurant_id, e)
        return None
    return fc if fc and fc.get("available") else None


def _service_order(hour):
    """Hours in the order a service runs: 11am … 11pm, then past midnight."""
    from time_utils import BUSINESS_DAY_START_HOUR
    h = int(hour)
    return h if h >= BUSINESS_DAY_START_HOUR else h + 24


def evening_share(hourly, net):
    """The share of the night's net rung at or after EVENING_HOUR (past
    midnight included), or None."""
    if not hourly or not net or net <= 0:
        return None
    late = sum(h["net"] for h in hourly if _service_order(h["hour"]) >= EVENING_HOUR)
    return round(late / net * 100.0, 1)


def _categories(ctx, by_department, net):
    """(categories, unmapped, unallocated). Categories in Erik's order, then
    any the owner named themselves, then nothing guessed."""
    mapping = store.category_map(ctx.restaurant_id, db_path=ctx.db_path)
    cats, unmapped = {}, []
    for dep, amount in sorted(by_department.items(), key=lambda kv: -kv[1]):
        cat = store.category_for(dep, mapping)
        if cat == dsr.UNMAPPED:
            unmapped.append({"department": dep, "net": round(amount, 2)})
            continue
        c = cats.setdefault(cat, {"category": cat, "net": 0.0, "departments": []})
        c["net"] = round(c["net"] + amount, 2)
        c["departments"].append(dep)
    order = [c for c in dsr.DEFAULT_CATEGORIES if c in cats] + sorted(c for c in cats if c not in dsr.DEFAULT_CATEGORIES)
    out = []
    for name in order:
        c = cats[name]
        c["share_pct"] = round(c["net"] / net * 100.0, 1) if net and net > 0 else None
        out.append(c)
    unallocated = round(net - sum(by_department.values()), 2) if net is not None else None
    return out, unmapped, (unallocated if unallocated and abs(unallocated) >= 0.01 else None)


def _items(items):
    sold = [it for it in items if (it.get("qty") or 0) > 0]
    top = sorted(sold, key=lambda it: (-it["net"], it["name"] or ""))[:TOP_N]
    names = {(it["name"], it["department"]) for it in top}
    bottom = [it for it in sorted(sold, key=lambda it: (it["net"], it["name"] or ""))
              if (it["name"], it["department"]) not in names][:TOP_N]
    return top, bottom


def collect(ctx):
    import ops
    import pos
    from time_utils import mdy
    provider, _mod = pos.connected_provider(ctx.restaurant_id)
    if not provider:
        return dsr.block(dsr.NOT_CONNECTED, reason=REASON_NO_POS, block_name="sales")

    closed = ctx.day_closed
    if closed is None:
        from dsr import pipeline
        closed = pipeline.day_closed(ctx.restaurant, ctx.business_date, ctx.now_utc, ctx.trigger) or False
    if not closed:
        return dsr.block(dsr.AWAITING, source=provider, block_name="sales",
                         reason=f"Awaiting the POS close for {mdy(ctx.business_date)}",
                         detail={"waiting_for": "pos_close"})

    try:
        data, provider = pos.fetch_day_sales(ctx.restaurant_id, ctx.business_date)
    except pos.POSCapabilityError as e:
        return dsr.block(dsr.UNAVAILABLE, source=provider, reason=REASON_CANT_REPORT, block_name="sales",
                         detail={"why": str(e)[:200]})
    except pos.POSAuthError as e:
        ops.capture(e, job="dsr_sales", context=f"restaurant_id={ctx.restaurant_id} auth")
        return dsr.block(dsr.UNAVAILABLE, source=provider, reason=REASON_AUTH, block_name="sales",
                         detail={"why": "credentials rejected"})
    except Exception as e:
        ops.capture(e, job="dsr_sales", context=f"restaurant_id={ctx.restaurant_id} business_date={ctx.day}")
        return dsr.block(dsr.AWAITING, source=provider, reason=REASON_PULL_FAILED, block_name="sales",
                         detail={"waiting_for": "pos_pull", "error": type(e).__name__})

    if not data["transactions"] and not data["gross"]:
        # The POS says the day is closed but holds no tickets for it: the
        # tickets have not reached the above-store copy yet, or there was no
        # service. Either way it is not a $0 night until the data says so.
        return dsr.block(dsr.AWAITING, source=provider, block_name="sales",
                         reason="The POS closed the day but its tickets haven't arrived yet",
                         detail={"waiting_for": "tickets", "closed_by": closed})
    return _ready(ctx, data, provider, closed)


# What the report calls GROSS, per restaurant (restaurants.dsr_gross_basis).
# NET is the same under both: items after discounts and comps, never tax or
# voids — Erik (Simple EJ's, 9/24/26): "gross sales = everything included;
# net sales = gross − comps, voids, tax etc." His net is our net; his gross
# adds the tax and the voided lines back. To verify against RPower's own
# totals (detail.source_checks) on the first live night.
GROSS_BASES = {
    "items": {"gross": "items at the price rung, before discounts and comps; no tax, tips, gift cards, "
                       "refunds or voids",
              "net": "gross less discounts and comps"},
    "all": {"gross": "everything rung: items at the price rung before discounts and comps, plus tax and "
                     "voided lines",
            "net": "gross less comps, discounts, voids and tax"},
}


def _gross(ctx, data):
    """(gross, basis, missing) for this restaurant's basis. An "all" gross
    whose tax or voids the POS did not report is not measured — never the
    items figure passed off as everything."""
    basis = getattr(ctx.restaurant, "dsr_gross_basis", None) or "items"
    if basis not in GROSS_BASES:
        basis = "items"
    items = data.get("gross")
    if basis == "items" or items is None:
        return items, basis, []
    missing = [k for k in ("tax", "voids") if data.get(k) is None]
    if missing:
        return None, basis, missing
    return round(float(items) + float(data["tax"]) + float(data["voids"]), 2), basis, []


def _ready(ctx, data, provider, closed_by):
    day = ctx.business_date
    net = data["net"]
    gross, gross_basis, gross_missing = _gross(ctx, data)
    tx, guests = data["transactions"], data["guests"]
    metrics = {
        "gross": gross, "gross_items": data["gross"], "net": net, "transactions": tx, "guests": guests,
        "avg_ticket": round(net / tx, 2) if tx else None,
        "per_guest": round(net / guests, 2) if guests else None,
        "discounts": data["discounts"], "comps": data["comps"], "voids": data["voids"],
        "refunds": data["refunds"], "tax": data["tax"],
    }
    baselines = {}
    for key, other in (("yesterday", day - timedelta(days=1)), ("last_week", day - timedelta(days=7)),
                       ("last_year", day - timedelta(days=LAST_YEAR_DAYS))):
        base, source = _baseline_net(ctx, other)
        metrics[f"{key}_net"] = base
        metrics[f"vs_{key}"] = _delta(net, base)
        metrics[f"vs_{key}_pct"] = _pct(net, base)
        baselines[key] = {"date": other.isoformat(), "net": base, "source": source}

    fc = _forecast(ctx)
    fc_net = float(fc["typical_sales"]) if fc else None
    metrics.update({"forecast_net": fc_net, "vs_forecast": _delta(net, fc_net), "vs_forecast_pct": _pct(net, fc_net)})
    baselines["forecast"] = ({"net": fc_net, "source": "demand.forecast_day", "samples": fc.get("samples"),
                              "basis": f"median of the last {fc.get('samples')} {fc.get('weekday')}s"}
                             if fc else {"net": None, "source": None})

    budget = store.budgets_for(ctx.restaurant_id, day, day, db_path=ctx.db_path).get(day.isoformat()) or {}
    b_gross, b_net = budget.get("gross"), budget.get("net")
    metrics.update({
        "budget_gross": b_gross, "vs_budget_gross": _delta(gross, b_gross), "vs_budget_gross_pct": _pct(gross, b_gross),
        "budget_net": b_net, "vs_budget_net": _delta(net, b_net), "vs_budget_net_pct": _pct(net, b_net),
    })

    hourly = [{"hour": h, "net": v} for h, v in sorted(data["by_hour"].items(), key=lambda kv: _service_order(kv[0]))]
    cats, unmapped, unallocated = _categories(ctx, data["by_department"], net)
    for c in cats:
        metrics[f"cat:{c['category']}"] = c["net"]
    if unmapped:
        metrics[f"cat:{dsr.UNMAPPED}"] = round(sum(u["net"] for u in unmapped), 2)
    metrics["evening_share_pct"] = evening_share(hourly, net)
    top, bottom = _items(data["items"])

    detail = {
        "provider": provider,
        "closed_by": closed_by,
        "definition": {"gross": GROSS_BASES[gross_basis]["gross"], "net": GROSS_BASES[gross_basis]["net"],
                       "gross_basis": gross_basis, "gross_missing": gross_missing,
                       "net_deductions": data.get("net_deductions") or []},
        "baselines": baselines,
        "budget": {"gross": b_gross, "net": b_net} if budget else None,
        "hourly": hourly,
        "categories": cats,
        "unmapped": unmapped,
        "unallocated": unallocated,
        "top_items": top,
        "bottom_items": bottom,
        "source_checks": data.get("source_checks") or {},
    }
    return dsr.block(dsr.READY, source=provider, metrics=metrics, detail=detail, block_name="sales")
