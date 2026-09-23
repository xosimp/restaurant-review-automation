"""
pos.py — one front door for every POS integration.

toast.py, square.py, and clover.py are three near-identical copy-pastes of the
same shape (is_connected / test_credentials / build_shifts_csv / sync_to_db),
and features kept landing in only one of them: the nightly labor sync was
Toast-only, so a Square or Clover client silently never got fresh shift data.
rpower.py was the first provider added this way. Adding another means
implementing this module's PROVIDER_API and adding one registry line — not
copying 500 lines and hoping every call site notices.
"""
import logging

log = logging.getLogger("pos")

# Each provider module must expose:
#   is_connected(restaurant_id) -> bool
#   sync_to_db(restaurant_id) -> {"ok": bool, ...}
#   build_shifts_csv(restaurant_id, days=60) -> str|None
PROVIDER_API = ("is_connected", "sync_to_db", "build_shifts_csv")

# The DATA reads, which only some providers can answer. These were never part
# of the contract, so cogs.py and inventory_ledger.py imported `toast`
# directly — which meant food cost %, recipe depletion and menu discovery were
# Toast-only features that silently reported "no POS connected" to a Square or
# Clover restaurant this module knew perfectly well was connected.
#
# Optional rather than required: Square and Clover expose daily sales totals
# but no item-level detail, and a provider that cannot answer must say so
# rather than return an empty list, because "nothing sold" and "I can't see
# what sold" lead to opposite conclusions everywhere downstream.
DATA_API = ("fetch_business_days", "fetch_order_selections", "fetch_loss_lines",
            "fetch_sales_today", "fetch_clock_ins_today", "fetch_order_customers")


def _load_providers():
    import toast, square, clover, rpower
    return {"toast": toast, "square": square, "clover": clover, "rpower": rpower}


# Populated lazily so tests can inject fakes and a broken provider import
# can't take down the app at boot.
PROVIDERS = None


def get_providers():
    global PROVIDERS
    if PROVIDERS is None:
        PROVIDERS = _load_providers()
    return PROVIDERS


def connected_provider(restaurant_id):
    """(name, module) for the first provider this restaurant is connected to,
    else (None, None)."""
    for name, mod in get_providers().items():
        try:
            if mod.is_connected(restaurant_id):
                return name, mod
        except Exception as e:
            log.error(f"pos.is_connected crashed for {name}: {e}")
    return None, None


def sync_restaurant(restaurant_id):
    """Sync whichever POS this restaurant uses. Uniform result shape."""
    name, mod = connected_provider(restaurant_id)
    if not mod:
        return {"ok": False, "provider": None, "error": "No POS connected"}
    try:
        result = mod.sync_to_db(restaurant_id) or {}
        result.setdefault("ok", False)
        result["provider"] = name
        return result
    except Exception as e:
        return {"ok": False, "provider": name, "error": str(e)}


POS_SYNC_MAX_SECONDS = 45 * 60


def sync_all():
    """Nightly: sync every restaurant that has ANY provider connected —
    not just Toast. One restaurant failing never blocks the rest."""
    from models import get_all_restaurants
    import ops
    import scheduler
    results = []
    # Paying or trialling restaurants only: a churned restaurant's POS was
    # still called every night (MOD-LAB-9). Bounded and resumable like the
    # review fetch: one long provider call no longer holds the whole pass,
    # and the next pass starts where this one stopped.
    live = {r.id: r for r in get_all_restaurants()
            if (getattr(r, "billing_status", None) or "trial").lower() in ("trial", "active", "past_due")}

    def _one(rid):
        r = live[rid]
        name, mod = connected_provider(rid)
        if not mod:
            return
        result = sync_restaurant(rid)
        results.append({"restaurant": r.name, **result})
        if result["ok"]:
            log.info(f"POS sync OK [{name}] {r.name} — {result.get('rows', '?')} rows")
        else:
            log.warning(f"POS sync failed [{name}] {r.name}: {result.get('error')}")
            ops.capture(Exception(result.get("error", "unknown")),
                        job="pos_sync", context=f"{name} {r.name}")
    scheduler.resumable_sweep("pos_sync", list(live), _one, max_seconds=POS_SYNC_MAX_SECONDS, job="pos_sync")
    return results


def save_synced_shifts(restaurant_id, csv_str, source):
    """Store a provider's synced window and archive its per-day history.

    Two things every provider did differently or not at all:
    - A sync covers ~60 days, and saving it replaced shifts_csv outright, so
      a year of hand-uploaded history was erased by the first nightly sync
      (MOD-LAB-18). Rows the owner had for dates OUTSIDE the synced window
      are kept; inside it, the POS is the record.
    - Only Toast archived labor_daily_history (what YoY and trends read), so
      Square and Clover restaurants never accumulated history (MOD-LAB-8).
    Returns the number of shift rows the synced window carried."""
    import csv as _csv
    import io as _io
    from labor import load_shifts
    from models import get_client_data, save_client_data
    new_rows = load_shifts(csv_string=csv_str)
    dates = sorted({r["date"] for r in new_rows if r.get("date")})
    merged = list(new_rows)
    if dates:
        lo, hi = dates[0], dates[-1]
        prior = (get_client_data(restaurant_id) or {}).get("shifts_csv") or ""
        if prior.strip():
            kept = [r for r in load_shifts(csv_string=prior) if not (lo <= (r.get("date") or "") <= hi)]
            merged = kept + merged
    merged.sort(key=lambda r: (r.get("date") or "", str(r.get("shift_start") or "")))
    fields = []
    for r in merged:
        for k in r:
            if k not in fields:
                fields.append(k)
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(merged)
    save_client_data(restaurant_id, "shifts", buf.getvalue(), source=source)
    try:
        from labor import analyse_shifts_for_restaurant
        from models import save_labor_daily_history, save_labor_snapshot
        # The per-day archive takes the whole synced file; the period
        # snapshot (what the labor alert reads) is the current window.
        save_labor_daily_history(restaurant_id, analyse_shifts_for_restaurant(
            restaurant_id, window_days=None).get("by_day", {}))
        analysis = analyse_shifts_for_restaurant(restaurant_id)
        dr = analysis.get("date_range", {})
        if dr.get("start") and dr.get("end"):
            save_labor_snapshot(restaurant_id, dr["start"], dr["end"], analysis["overall_labor_pct"],
                                analysis["total_labor_cost"], analysis["total_sales"])
    except Exception as e:
        log.warning(f"[{source} sync] daily history archive error: {e}")
    return len(new_rows)


def connection_status(restaurant_id):
    """Uniform status for UI: which provider, connected or not."""
    name, mod = connected_provider(restaurant_id)
    return {"connected": bool(mod), "provider": name}


# ── data reads ──────────────────────────────────────────────────────────────

class POSCapabilityError(Exception):
    """The connected POS cannot answer this question.

    Distinct from "there is no POS" and from "the POS returned nothing",
    because all three need different words in front of an owner: connect
    something, connect something that reports this, or you genuinely sold
    nothing that day.
    """


def supports(restaurant_id, capability):
    """Whether the connected provider can answer `capability`."""
    _name, mod = connected_provider(restaurant_id)
    return bool(mod and hasattr(mod, capability))


def fetch_business_days(restaurant_id, start_date, end_date):
    """{'YYYY-MM-DD': net_sales} from whichever POS is connected.

    Returns (data, provider_name). Raises POSCapabilityError when a POS is
    connected but cannot report daily sales — never an empty dict, which
    would read downstream as a restaurant that took no money.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_business_days", None)
    if fn is None:
        raise POSCapabilityError(f"{name} does not report daily sales through Cavnar yet")
    return fn(restaurant_id, start_date, end_date), name


def fetch_order_selections(restaurant_id, business_date):
    """One business date's sold items as [{"item": {"guid": ...}, "quantity": n}].

    Returns (rows, provider_name). Item-level detail is what recipe depletion
    and menu discovery are built on; a provider without it raises rather than
    returning [], because an empty list makes every ingredient look unused and
    quietly stops the reorder list from ever flagging anything.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_order_selections", None)
    if fn is None:
        raise POSCapabilityError(
            f"{name} does not report item-level sales through Cavnar yet, so recipe "
            f"depletion and menu discovery cannot run for this restaurant")
    return fn(restaurant_id, business_date), name


def fetch_order_customers(restaurant_id, business_date):
    """One business date's identified guests as [{name, phone, email, order_guid}].

    Returns (rows, provider_name). Only a provider that exposes customer
    records can answer; RPOWER's customer scope is a separate agreement
    (see the memory note), so until it lands this raises for RPOWER and
    the callers say "not available on this POS" rather than counting zero.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_order_customers", None)
    if fn is None:
        raise POSCapabilityError(
            f"{name} does not share guest records through Cavnar yet, so campaign "
            f"visit matching and opt-in invites cannot run for this restaurant")
    return fn(restaurant_id, business_date), name


def fetch_loss_lines(restaurant_id, start_date, end_date):
    """Comps, voids and refunds per line, from whichever POS can report them.

    Returns (rows, provider_name). Raises POSCapabilityError when the POS
    cannot — only RPOWER's documented ticketsales carries the approving
    manager (mgr_mid / voidmgr_mid) today — rather than returning [], which
    would read as a restaurant that never comped anything.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_loss_lines", None)
    if fn is None:
        raise POSCapabilityError(f"{name} does not report comps and voids yet")
    return fn(restaurant_id, start_date, end_date), name


def fetch_sales_today(restaurant_id, business_date):
    """Net sales SO FAR today, from a POS that can be asked during service.

    Returns (net_sales, provider). Raises POSCapabilityError where the POS
    cannot answer intraday — RPOWER's API is month-at-a-time (vendor
    confirmed), so a mid-service figure from it does not exist. Never
    returns 0 for "don't know": a restaurant told it has done no business
    by 5pm would act on it.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_sales_today", None)
    if fn is None:
        raise POSCapabilityError(f"{name} cannot be read during service")
    return fn(restaurant_id, business_date), name


def fetch_clock_ins_today(restaurant_id, business_date):
    """Who has clocked in today: [{"employee", "role", "clocked_in_at"}].

    Returns (rows, provider); raises POSCapabilityError where the POS has no
    live labor feed.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_clock_ins_today", None)
    if fn is None:
        raise POSCapabilityError(f"{name} has no live clock-in feed")
    return fn(restaurant_id, business_date), name
