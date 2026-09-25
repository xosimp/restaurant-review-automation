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
import threading
import time

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
            "fetch_sales_today", "fetch_clock_ins_today", "fetch_order_customers",
            "fetch_day_sales", "fetch_day_closed")

# ── The sales contract every provider's synced CSV keeps (CA3 F11) ─────────
#
# build_shifts_csv's `sales` column — what labor_daily_history, labor %,
# cogs' archive, demand and every cohort benchmark read — is, for EVERY
# provider:
#
#   * NET sales: items at the price rung, less discounts and comps
#     (NET_DEDUCTIONS). Never tax, tips, gratuities or service charges.
#       toast   — businessDays' net (toast.py fetch_business_days)
#       rpower  — the sales-type rules in rpower.fetch_business_days
#       square  — order total_money less total_tax_money, total_tip_money
#                 and total_service_charge_money (square._order_net_cents)
#       clover  — order total less the tax on its payments
#                 (clover._order_net_cents); verify on the first live
#                 Clover restaurant, there is none today
#   * by BUSINESS DATE in the restaurant's timezone: an order or a shift
#     is filed under the service it belongs to (time_utils.business_date —
#     before BUSINESS_DAY_START_HOUR it is still last night), never the UTC
#     calendar date. Shift start/end times are local.
#   * dollars to the cent, and blank — never 0 — for a day with no figure.
#   * a page that fails mid-range fails the sync (nothing is saved), so a
#     partial day is never stored as a whole one (Square MOD-LAB-7, Clover).


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
    """(name, module) for the provider this restaurant's POS data comes from,
    else (None, None). With credentials for more than one (a migration), the
    ONE primary provider pos_health.primary_provider names — the same one
    every freshness reading reads — so the nightly sync, the DSR, depletion
    and the freshness line never read different providers (DH2-13)."""
    connected = []
    for name, mod in get_providers().items():
        try:
            if mod.is_connected(restaurant_id):
                connected.append((name, mod))
        except Exception as e:
            log.error(f"pos.is_connected crashed for {name}: {e}")
    if not connected:
        return None, None
    if len(connected) == 1:
        return connected[0]
    try:
        import pos_health
        from models import get_restaurant
        pick = pos_health.primary_provider(get_restaurant(restaurant_id), [n for n, _m in connected])
    except Exception as e:
        log.warning(f"primary POS provider unreadable for {restaurant_id}: {e}")
        pick = None
    return next(((n, m) for n, m in connected if n == pick), connected[0])


# ── HTTP with retries (Toast, Square, Clover) ───────────────────────────────
#
# Toast, Square and Clover each made one attempt per call, so one 429 or 503
# at 3:02am failed a 60-day sync until 3am the next night (DH2-5). RPOWER has
# its own backoff (rpower._request). Retried: a connection error or timeout,
# 429 and 5xx — with Retry-After honoured, capped. Never a 401 or 403: bad
# credentials are bad on the third try too, and the owner has to reconnect.
HTTP_TRIES = 3
HTTP_BACKOFF_SECONDS = 1.0
HTTP_RETRY_AFTER_MAX = 30.0
_sleep = time.sleep          # module-level so a test can skip the waits


def http_call(fn, url, *, timeout, tries=HTTP_TRIES, **kwargs):
    """`fn(url, timeout=timeout, **kwargs)` — requests.get or requests.post,
    passed resolved at the call site — with up to `tries` attempts. Returns
    the last response (the caller still raise_for_status()es it); re-raises
    the last connection error. Every call names its timeout (CLAUDE.md)."""
    import requests
    for attempt in range(max(1, tries)):
        last = attempt + 1 >= max(1, tries)
        wait = HTTP_BACKOFF_SECONDS * (2 ** attempt)
        try:
            resp = fn(url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            _sleep(wait)
            continue
        status = getattr(resp, "status_code", 200) or 200
        if (status == 429 or status >= 500) and not last:
            try:
                ra = float((getattr(resp, "headers", None) or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            _sleep(min(HTTP_RETRY_AFTER_MAX, max(wait, ra)))
            continue
        return resp
    return resp


# ── when a business day is complete (DH2-3) ─────────────────────────────────

def complete_through(restaurant, now_local=None):
    """The last business date that had ENDED at `now_local` (the
    restaurant's naive local time): its close from the restaurant's hours
    when it has them, else the BUSINESS_DAY_START_HOUR boundary. A pull at
    3am Chicago is 1am in Los Angeles — the night before is still trading,
    and a day read before it closed must not be stored or dated as whole."""
    from datetime import timedelta
    from time_utils import restaurant_now, business_date, open_service_day, service_window, \
        BUSINESS_DAY_START_HOUR
    local = now_local or restaurant_now(restaurant, naive=True)
    day = business_date(restaurant, local)
    if open_service_day(restaurant, local) is None and local.hour < BUSINESS_DAY_START_HOUR:
        try:
            window = service_window(restaurant, day)
        except Exception:
            window = None
        if window and window[1] <= local:
            return day                   # it closed already: last night is whole
    return day - timedelta(days=1)


def _complete_through_for(restaurant_id):
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return complete_through(r) if r is not None else None
    except Exception:
        return None


# Automatic recovery (DH5-7): a failed sync is retried this many hours after
# each failure — +1h after the nightly one, then +3h, then +6h — by the
# hourly retry sweep (scheduler.run_pos_retry, until 11am local). Never
# after an auth failure: that waits for the owner to reconnect.
RETRY_DELAYS_HOURS = (1, 3, 6)


def sync_restaurant(restaurant_id, trigger="nightly", attempt=0):
    """Sync whichever POS this restaurant uses. Uniform result shape. Every
    attempt — the nightly sweep, a retry, a "Sync now" — is recorded in the
    Data Health ledger (data_health.record_attempt), so a sync that failed or
    never ran is never read as a quiet night. data_through is the last
    COMPLETE business date the pull covered (complete_through), and a failure
    sets next_retry_at from RETRY_DELAYS_HOURS[attempt] (0 = the first)."""
    import time as _time
    from datetime import datetime, timedelta, timezone
    name, mod = connected_provider(restaurant_id)
    if not mod:
        return {"ok": False, "provider": None, "error": "No POS connected"}
    t0 = _time.monotonic()
    try:
        result = mod.sync_to_db(restaurant_id) or {}
        result.setdefault("ok", False)
        result["provider"] = name
    except Exception as e:
        result = {"ok": False, "provider": name, "error": str(e)}
    try:
        import data_health
        ok = bool(result.get("ok"))
        err = None if ok else result.get("error")
        klass = None if ok else ("auth" if result.get("auth") else data_health.classify_error(err))
        retry_at = None
        if not ok and klass != "auth" and 0 <= int(attempt or 0) < len(RETRY_DELAYS_HOURS):
            retry_at = (datetime.now(timezone.utc) + timedelta(hours=RETRY_DELAYS_HOURS[int(attempt or 0)])
                        ).strftime("%Y-%m-%d %H:%M:%S")
        through = result.get("data_through") or (_complete_through_for(restaurant_id) if ok else None)
        data_health.record_attempt(restaurant_id, "pos", ok, provider=name, error=err, error_class=klass,
                                   data_through=through, next_retry_at=retry_at,
                                   duration_ms=int((_time.monotonic() - t0) * 1000))
    except Exception as e:
        log.warning(f"POS sync attempt not recorded for {restaurant_id}: {e}")
    if result.get("ok"):
        _recheck_sales_consistency(restaurant_id)
    return result


def _recheck_sales_consistency(restaurant_id):
    """After a successful pull: final nightly reports whose net no longer
    matches the POS archive (a late void, a check closed after the report)
    reach the operator (DH2-18). The owner-facing half is the `sales`
    freshness source, which reads the same check (data_freshness
    .sales_consistency). Never raises."""
    try:
        import data_freshness
        chk = data_freshness.sales_consistency(restaurant_id)
        if chk.get("mismatches"):
            import ops
            m = chk["mismatches"][0]
            ops.capture(RuntimeError(f"Daily report net ${m['dsr']:,.2f} vs POS ${m['pos']:,.2f} on {m['date']} "
                                     f"({len(chk['mismatches'])} night(s) apart)"),
                        job="dsr_pos_consistency", context=f"restaurant_id={restaurant_id}")
            _reopen_moved_nights(restaurant_id, chk["mismatches"])
    except Exception as e:
        log.warning(f"sales consistency re-check skipped for {restaurant_id}: {e}")


def _reopen_moved_nights(restaurant_id, mismatches):
    """Recent final nights whose POS total moved after the report (dsr D1-1):
    each is re-pulled off this thread (dsr.pipeline.recheck_final — a new
    version only when the POS's own figure changed). Only where the archive
    is built as the DSR's net, so a mismatch means the POS moved rather than
    two ways of counting; only nights the sweep still looks at."""
    from datetime import date, timedelta
    from dsr import pipeline, store
    from models import get_restaurant
    name, _mod = connected_provider(restaurant_id)
    if not store.pos_sync_same_basis(name):
        return
    r = get_restaurant(restaurant_id)
    if r is None or not getattr(r, "dsr_enabled", 1):
        return
    since = date.today() - timedelta(days=pipeline.SWEEP_LOOKBACK_DAYS)
    nights = sorted({m["date"] for m in mismatches if str(m.get("date") or "") >= since.isoformat()})
    if not nights:
        return

    def go():
        for night in nights:
            pipeline.recheck_final(r, night)
    pipeline._spawn(go)


POS_SYNC_MAX_SECONDS = 45 * 60
# The fetch is network-bound (a 60-day pull per restaurant); the SQLite write
# section (save_synced_shifts) holds _WRITE_LOCK, so four fetches overlap and
# one restaurant at a time writes (DH5-7).
POS_SYNC_WORKERS = int(__import__("os").getenv("POS_SYNC_WORKERS", "4"))
_WRITE_LOCK = threading.RLock()


def sync_all(stats=None):
    """Nightly: sync every restaurant in service (models.in_service — the
    one eligibility rule every sync job uses, DH2-12) that has ANY provider
    connected. One restaurant failing never blocks the rest. Returns one
    result per restaurant attempted, in sweep order; `stats` (a dict), when
    given, is filled with {attempted, ok, failed, skipped, hit_bound} for
    job_runs."""
    from models import get_all_restaurants, in_service
    import ops
    import scheduler
    by_rid, lock = {}, threading.Lock()
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    # Bounded and resumable like the review fetch: one long provider call no
    # longer holds the whole pass, and the next pass starts where this one
    # stopped.
    live = {r.id: r for r in get_all_restaurants() if in_service(r)}

    def _one(rid):
        r = live[rid]
        name, mod = connected_provider(rid)
        if not mod:
            with lock:
                counts["skipped"] += 1
            return
        result = sync_restaurant(rid)
        with lock:
            by_rid[rid] = {"restaurant": r.name, **result}
            counts["attempted"] += 1
            counts["ok" if result["ok"] else "failed"] += 1
        if result["ok"]:
            log.info(f"POS sync OK [{name}] {r.name} — {result.get('rows', '?')} rows")
        else:
            log.warning(f"POS sync failed [{name}] {r.name}: {result.get('error')}")
            ops.capture(Exception(result.get("error", "unknown")),
                        job="pos_sync", context=f"restaurant_id={rid} {name} {r.name}")
        try:
            note_sync_failure(rid)
        except Exception as e:
            log.warning(f"POS sync-failure note skipped for {rid}: {e}")
    order = scheduler._fetch_order(list(live), key="pos_sync")
    _done, hit_bound = scheduler.resumable_sweep("pos_sync", list(live), _one, max_seconds=POS_SYNC_MAX_SECONDS,
                                                 workers=POS_SYNC_WORKERS, job="pos_sync")
    if hit_bound:
        # The restaurants not reached tonight lead the next pass (the
        # cursor), and the retry sweep picks up nothing for them — so the
        # operator hears it (DH2-5).
        ops.capture(RuntimeError(f"POS sync covered {counts['attempted'] + counts['skipped']} of {len(live)} "
                                 f"restaurants before the {POS_SYNC_MAX_SECONDS}s bound; the rest lead the "
                                 "next pass"), job="pos_sync", context="time_bound")
    counts["hit_bound"] = bool(hit_bound)
    if isinstance(stats, dict):
        stats.update(counts)
    return [by_rid[rid] for rid in order if rid in by_rid]


# A sync failing this long, with no success in between, is the owner's to
# know about — not only the operator's failure digest.
SYNC_FAILURE_NOTICE_DAYS = 2


def note_sync_failure(restaurant_id, now=None):
    """Record an account-visible activity event ("pos_sync_failing") when the
    restaurant's POS — any provider, RPOWER included — has been failing for
    SYNC_FAILURE_NOTICE_DAYS or more with no successful sync in between.
    Once per failure run: nothing is written again until a sync succeeds.
    An activity event on the account, deliberately not an SMS or a push
    (CA3 F6). Returns True when it wrote one."""
    from datetime import datetime, timezone, timedelta
    import models
    import pos_health
    r = models.get_restaurant(restaurant_id)
    if r is None:
        return False
    st = pos_health.pos_sync_state(r, now=now)
    if st["state"] != "error":
        return False
    now = now or datetime.now(timezone.utc)
    last_ok = pos_health.parse_stamp(st.get("last_synced"))
    if last_ok is not None and (now - last_ok) < timedelta(days=SYNC_FAILURE_NOTICE_DAYS):
        return False
    conn = models.get_conn()
    try:
        rows = conn.execute("SELECT created_at FROM activity_log WHERE restaurant_id=? "
                            "AND event_type='pos_sync_failing' ORDER BY id DESC LIMIT 1",
                            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    if rows:
        from time_utils import parse_stamp
        prev = parse_stamp(rows[0]["created_at"], naive_tz="America/Chicago")
        if last_ok is None or (prev is not None and prev >= last_ok):
            return False            # already told about this run of failures
    name = {"rpower": "RPOWER"}.get(st["provider"], (st["provider"] or "POS").title())
    from time_utils import mdy
    since = f" since {mdy(last_ok.date())}" if last_ok else ""
    models.log_event(restaurant_id, "pos_sync_failing", {
        "provider": st["provider"],
        "detail": (f"{name} hasn't synced{since} — {str(st.get('error') or '')[:160]}. "
                   "Labor and sales figures stop at the last good sync until it reconnects."),
        "error": str(st.get("error") or "")[:300],
        "last_synced": st.get("last_synced"),
    })
    return True


def _serialized_write(fn):
    """Hold _WRITE_LOCK for the whole call: the nightly sweep fetches four
    restaurants at once, and this is the section that writes SQLite."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _WRITE_LOCK:
            return fn(*args, **kwargs)
    return wrapper


@_serialized_write
def save_synced_shifts(restaurant_id, csv_str, source):
    """Store a provider's synced window and archive its per-day history.

    Two things every provider did differently or not at all:
    - A sync covers ~60 days, and saving it replaced shifts_csv outright, so
      a year of hand-uploaded history was erased by the first nightly sync
      (MOD-LAB-18). Rows the owner had for dates OUTSIDE the synced window
      are kept; inside it, the POS is the record.
    - Only Toast archived labor_daily_history (what YoY and trends read), so
      Square and Clover restaurants never accumulated history (MOD-LAB-8).
    Returns the number of shift rows the synced window carried. Serialized
    by _WRITE_LOCK (the sweep's fetches run four at a time)."""
    import csv as _csv
    import io as _io
    from datetime import datetime, timezone
    from labor import load_shifts, drop_future_shifts
    from models import get_client_data, save_client_data
    # A provider row dated after the restaurant's today is a clock or data
    # error, never stored (re-audit B3#4 — the upload refuses it too).
    new_rows = drop_future_shifts(load_shifts(csv_string=csv_str), restaurant_id=restaurant_id)
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
        from labor import full_history_by_day
        # Provenance for the days this pull covered (DH1-9): which provider,
        # when, and whether the business day had ENDED when it was read — a
        # day still trading at pull time is stored provisional (final=0), and
        # freshness dates the POS by its complete days only (DH2-3).
        ct = _complete_through_for(restaurant_id)
        prov = {"source": source, "provider": source,
                "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "window": (dates[0], dates[-1]) if dates else None,
                "complete_through": ct.isoformat() if ct else None}
        save_labor_daily_history(restaurant_id, full_history_by_day(restaurant_id), provenance=prov)
        analysis = analyse_shifts_for_restaurant(restaurant_id)
        dr = analysis.get("date_range", {})
        if dr.get("start") and dr.get("end"):
            save_labor_snapshot(restaurant_id, dr["start"], dr["end"], analysis["overall_labor_pct"],
                                analysis.get("costed_labor", analysis["total_labor_cost"]), analysis["total_sales"])
    except Exception as e:
        log.warning(f"[{source} sync] daily history archive error: {e}")
    # The synced figures just changed: the cached Labor read (and Home and
    # Ask, which invalidate_insight_cache clears with it) must not narrate
    # the pre-sync numbers for the rest of its window (DH4-21, #46).
    try:
        import client_api
        client_api.invalidate_insight_cache(restaurant_id)
    except Exception as e:
        log.warning(f"[{source} sync] insight cache not cleared: {e}")
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
        raise POSCapabilityError(f"{name} does not report daily sales through Cavnar AI yet")
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
            f"{name} does not report item-level sales through Cavnar AI yet, so recipe "
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
            f"{name} does not share guest records through Cavnar AI yet, so campaign "
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


# ── the nightly DSR's reads (dsr/) ──────────────────────────────────────────

# What comes off GROSS to make NET, by name. The owner's own definition is
# still open (docs/plans/DSR_ENGINE_PLAN.md §11 Q2: "what comes off gross —
# comps, discounts, voids, tax?"), so it lives here once: every provider
# returns the parts, and this tuple alone decides the subtraction for the
# day, each department, each hour and each item. Drop "comps" to keep comps
# in net; a new name must be a part every provider reports.
NET_DEDUCTIONS = ("discounts", "comps")


class POSAuthError(Exception):
    """The POS rejected Cavnar's credentials. Not a capability gap and not a
    blip: retrying will not help until someone reconnects it."""


def _auth_failure(exc) -> bool:
    if type(exc).__name__.endswith("AuthError"):          # rpower.RPowerAuthError
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (401, 403)                           # a requests.HTTPError from Toast


def net_of(parts) -> float:
    """GROSS less every NET_DEDUCTIONS figure present in `parts`. A deduction
    a provider cannot separate is None and subtracts nothing here — it is
    already inside another one (Toast rings comps as discounts)."""
    parts = parts or {}
    gross = float(parts.get("gross") or 0.0)
    off = sum(float(parts[k]) for k in NET_DEDUCTIONS if parts.get(k) is not None)
    return round(gross - off, 2)


def _money(v):
    return None if v is None else round(float(v), 2)


def fetch_day_sales(restaurant_id, business_date):
    """One business date's sales, for the DSR.

    Returns (data, provider_name), where data is
      {"gross", "net", "transactions", "guests", "discounts", "comps",
       "voids", "refunds", "tax", "by_department": {pos department: net},
       "by_hour": {"HH": net}, "items": [{"name", "department", "guid", "qty", "net"}],
       "net_deductions", "source_checks"}
    An item's `guid` is the POS's own item id (Toast's item guid, RPOWER's
    menuitem_mid) — what menu_items.toast_guid holds — or None.

    GROSS is every item sold, at the price it was rung, before any discount
    or comp: the POS's sale lines plus the value of comped items. It never
    includes tax, tips or gratuities, service fees, non-sale lines (gift
    cards, deposits, pay-ins), refunds or voided lines.

    NET is GROSS minus exactly the figures named in NET_DEDUCTIONS — today
    discounts and comps. Tax, voids and refunds are reported beside it and
    never subtracted: a voided line was never a sale, tax is not the
    restaurant's money, and a refund is its own event, not a negative sale
    of tonight's. On RPOWER this net equals fetch_business_days' figure (the
    same lines through the same sales-type rules), so the DSR and every
    other surface agree.

    `guests` is None when the POS does not track covers; `comps` is None
    where the POS rings comps as discounts (Toast). by_department, by_hour
    and items are netted by the same rule. `source_checks` carries the POS's
    own totals beside ours, for verifying field semantics on a first live
    night.

    Raises POSCapabilityError when there is no POS or it cannot report a day
    this way (never an empty result, which would read as a night with no
    sales), POSAuthError when the POS rejected the credentials, and lets
    anything else the provider raised through as a transient failure the
    caller may retry.
    """
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_day_sales", None)
    if fn is None:
        raise POSCapabilityError(f"{name} does not report a day's sales detail through Cavnar AI yet")
    try:
        raw = fn(restaurant_id, business_date)
    except NotImplementedError as e:
        # Connected, but unable to answer for THIS restaurant (Toast demo
        # mode) — a capability gap, not a failure.
        raise POSCapabilityError(str(e) or f"{name} cannot report this day")
    except Exception as e:
        if _auth_failure(e):
            raise POSAuthError(str(e)) from e
        raise
    return _net_day(raw), name


def _net_day(raw):
    """A provider's parts, netted by NET_DEDUCTIONS in one place."""
    total = {k: raw.get(k) for k in ("gross", "discounts", "comps")}
    items = [{"name": it.get("name"), "department": it.get("department"), "guid": it.get("guid"),
              "qty": round(float(it.get("qty") or 0), 3), "net": net_of(it.get("parts"))}
             for it in raw.get("items") or []]
    return {
        "gross": _money(raw.get("gross") or 0.0),
        "net": net_of(total),
        "transactions": int(raw.get("transactions") or 0),
        "guests": int(raw["guests"]) if raw.get("guests") else None,
        "discounts": _money(raw.get("discounts")),
        "comps": _money(raw.get("comps")),
        "voids": _money(raw.get("voids")),
        "refunds": _money(raw.get("refunds")),
        "tax": _money(raw.get("tax")),
        "by_department": {k: net_of(v) for k, v in (raw.get("by_department") or {}).items()},
        "by_hour": {k: net_of(v) for k, v in sorted((raw.get("by_hour") or {}).items())},
        "items": items,
        "net_deductions": list(NET_DEDUCTIONS),
        "source_checks": raw.get("source_checks") or {},
    }


def fetch_day_closed(restaurant_id, business_date):
    """Whether the POS itself has closed `business_date` (RPOWER's closeday
    record). Returns (closed, provider_name).

    Raises POSCapabilityError where the POS keeps no such record — the DSR
    pipeline then treats the day as closed a grace period after the
    restaurant's close time — and lets a transient failure propagate, so
    "couldn't ask" is never read as either answer."""
    name, mod = connected_provider(restaurant_id)
    if not mod:
        raise POSCapabilityError("no POS connected")
    fn = getattr(mod, "fetch_day_closed", None)
    if fn is None:
        raise POSCapabilityError(f"{name} keeps no close-day record Cavnar AI can read")
    return bool(fn(restaurant_id, business_date)), name
