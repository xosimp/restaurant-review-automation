"""rpower.py — RPOWER Core API client.

RPOWER's Above-Store database, read through https://rpowerpos.com/api/v1/coreapi/.
Implements the same contract as toast.py/square.py/clover.py so pos.py can
treat it as one more provider, plus the item-level reads Food Cost needs.

Three things in RPOWER's own documentation shape this file, and each is a
trap that would otherwise produce confidently wrong numbers:

1.  **`time_stamp` is not business time.** RPOWER says so outright: "The
    time_stamp value represents the time a record was written or updated.
    This value will be changed any time data is reposted from RPOWER at the
    store. When seeking records associated with a particular day/time
    associated with business activity, rely on date values instead."

    So every analytic read here uses a `getbybusinessdate` endpoint. The
    `getbytimestamp` variants exist and are useful — but only as an
    incremental-sync cursor ("what changed since I last looked"), never as
    "what happened last week". This is the same mistake the Reviews module
    made with fetched_at and the Food Cost module made with its count dates.

2.  **Ticket ITEMS are not sales.** Also RPOWER's own words: "Ticket Items
    can be used to reconstruct what displays on a receipt, but it should not
    be used for calculating sales totals. Use the Ticket Sales set of
    endpoints to calculate sales totals and quantities." `ticketitem` rows
    include modifier lines (mod_level > 0, parent_atom pointing at the
    parent, qty 1, total 0), so summing them as units-sold counts a burger
    with four modifiers as five items. Everything here reads `ticketsales`.

3.  **Omitting `pagenumber` silently truncates at 1000 rows.** And RPOWER's
    own description of the parameter contradicts itself — "Sending a 1 ...
    will get you the first 1000 records. Sending a 2 will get you records
    2000-2999" skips 1000-1999. `_paged` below does not trust either reading:
    it pages until a short page arrives and detects overlap by primary key,
    so an off-by-one-page API returns correct data and a duplicate-emitting
    one is caught rather than double-counted. See `PAGINATION_UNVERIFIED`.

Not documented anywhere in RPOWER's 120 endpoints: rate limits, and any
response code other than 200. Both are handled defensively here and both are
open questions with RPOWER.
"""
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger("rpower")

BASE_URL = "https://rpowerpos.com/api/v1/coreapi"

# RPOWER's intro links these endpoints over http://. Every example in the
# collection uses https, and a bearer token over plaintext is not acceptable
# regardless — so the base above is https and this module never downgrades.
PAGE_SIZE = 1000

# RPOWER's pagination description is self-contradictory (see module docstring).
# Until it is confirmed against a real account with >1000 rows in one day,
# _paged verifies by primary key rather than trusting the page arithmetic, and
# this flag stays True so the verification cost is a deliberate, visible
# choice rather than a forgotten one.
# Still unconfirmed. RPOWER has not given us rate limits or the non-200
# response shapes, and there is NO SANDBOX — confirmed 18 Sep 2026, access is
# read-only against live stores. So the first real call is against Erik's
# production data, and every defensive choice in this file stays until the
# behaviour is observed rather than assumed.
PAGINATION_UNVERIFIED = True

# No rate limit is documented. These are conservative defaults chosen so a
# full historical backfill cannot look like an attack; revise once RPOWER
# confirms the real ceiling.
REQUEST_SPACING_SECONDS = 0.15
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0
TIMEOUT_SECONDS = 45

# A backfill asks for one business date at a time for item-level data (a busy
# day can exceed a single page) but takes date ranges where the API allows.
#
# CONFIRMED by RPOWER (Justin, 18 Sep 2026): "Month worth of data at a time."
# Both range fetches below chunk through _chunk_range, so a 60-day sync is two
# requests rather than one oversized one.
MAX_RANGE_DAYS = 31


class RPowerError(Exception):
    """Any failure reaching or reading RPOWER."""


class RPowerAuthError(RPowerError):
    """The token is missing, expired or not authorised for this store.

    Separate from RPowerError because the caller's response is different: a
    transport failure is worth retrying, a rejected token is worth telling
    the owner about and never retrying on a loop.
    """


# ── credentials ─────────────────────────────────────────────────────────────

def _creds(restaurant_id: int) -> dict:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r:
        raise RPowerAuthError("no such restaurant")
    return {
        "token": (getattr(r, "rpower_token", None) or "").strip(),
        "cg": getattr(r, "rpower_cg", None),
        "store_mid": (getattr(r, "rpower_store_mid", None) or "").strip() or None,
        "name": getattr(r, "rpower_store_name", None),
    }


def is_connected(restaurant_id: int) -> bool:
    """A token alone is not a connection.

    cg and store_mid come back from /store/get and are required by every
    subsequent call, so a restaurant with a pasted-but-unverified token is
    not connected — it is halfway through setup, and treating it as connected
    is how a sync job starts failing silently every night.
    """
    try:
        c = _creds(restaurant_id)
    except RPowerError:
        return False
    return bool(c["token"] and c["cg"] and c["store_mid"])


def has_token(restaurant_id: int) -> bool:
    """A token is on file, whether or not bootstrap has run."""
    try:
        return bool(_creds(restaurant_id)["token"])
    except RPowerError:
        return False


# ── transport ───────────────────────────────────────────────────────────────

_last_request_at = [0.0]


def _sleep_for_spacing():
    gap = time.monotonic() - _last_request_at[0]
    if gap < REQUEST_SPACING_SECONDS:
        time.sleep(REQUEST_SPACING_SECONDS - gap)
    _last_request_at[0] = time.monotonic()


def _request(token: str, path: str, params: dict = None) -> object:
    """One GET against the Core API, with retries on the failures worth
    retrying and none on the failures that will not improve.

    RPOWER documents only 200 responses across all 120 endpoints, so the
    status handling here is defensive rather than specified: anything 2xx is
    taken as success, 401/403 raise RPowerAuthError (never retried — a bad
    token is bad on the third attempt too), 429 and 5xx back off, and a
    non-JSON body is an error rather than something to guess at.
    """
    import requests

    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last_error = None
    for attempt in range(MAX_RETRIES):
        _sleep_for_spacing()
        try:
            resp = requests.get(url, headers=headers, params=params or {},
                                timeout=TIMEOUT_SECONDS)
        except Exception as e:
            last_error = f"request failed: {e}"
            time.sleep(RETRY_BACKOFF ** attempt)
            continue

        if resp.status_code in (401, 403):
            raise RPowerAuthError(
                f"RPOWER rejected the token ({resp.status_code}). Check that the token is "
                f"current and that it is authorised for this store.")
        if resp.status_code == 429 or resp.status_code >= 500:
            # Undocumented but assumed to exist. Honour Retry-After when the
            # server sends one rather than guessing over the top of it.
            wait = RETRY_BACKOFF ** attempt
            try:
                wait = max(wait, float(resp.headers.get("Retry-After", 0)))
            except (TypeError, ValueError):
                pass
            last_error = f"HTTP {resp.status_code}"
            time.sleep(wait)
            continue
        if not resp.ok:
            raise RPowerError(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise RPowerError(f"{path} returned a non-JSON body: {resp.text[:200]}")

    raise RPowerError(f"{path} failed after {MAX_RETRIES} attempts: {last_error}")


def _row_key(row: dict) -> str:
    """A stable identity for a record, for the overlap check in _paged.

    RPOWER's tables use `rid` for transactional records and `mid` for
    configuration records; `atom` distinguishes line items within a ticket.
    Falls back to the whole row so an unkeyed table still de-duplicates
    correctly rather than silently dropping distinct records.
    """
    for k in ("rid", "mid"):
        if row.get(k):
            return f"{k}:{row[k]}:{row.get('atom', '')}"
    import json as _j
    return _j.dumps(row, sort_keys=True, default=str)


def _paged(token: str, path: str, params: dict, max_pages: int = 200) -> list:
    """Every page of a result set, with the pagination ambiguity handled.

    RPOWER says omitting `pagenumber` returns only the first 1000 records —
    silently, with nothing in the response to say more exist. It then
    describes page 2 as "records 2000-2999", which would skip 1000-1999.
    Either the prose is wrong or the API is, and getting it wrong means a
    busy Saturday's sales are quietly half-missing.

    So this does not rely on the arithmetic at all. It walks pages from 1,
    stops on the first short page, and keys every row: a page that repeats
    rows already seen means the server is not advancing the way the caller
    assumed, and that is raised rather than returned as a doubled total.
    """
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        body = _request(token, path, {**params, "pagenumber": page})
        rows = body if isinstance(body, list) else ([body] if body else [])
        if not rows:
            break

        fresh = []
        duplicates = 0
        for row in rows:
            if not isinstance(row, dict):
                fresh.append(row)
                continue
            key = _row_key(row)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            fresh.append(row)

        # A fully repeated page means paging is not advancing — returning
        # what we have is correct, continuing would loop forever.
        if duplicates and not fresh:
            log.warning("[rpower] %s page %s repeated entirely — stopping", path, page)
            break
        if duplicates:
            log.warning("[rpower] %s page %s overlapped the previous page by %s rows "
                        "— RPOWER's page arithmetic is not what the docs describe",
                        path, page, duplicates)
        out.extend(fresh)
        if len(rows) < PAGE_SIZE:
            break
    else:
        log.warning("[rpower] %s hit the %s-page ceiling — result may be truncated",
                    path, max_pages)
    return out


def _d(value) -> str:
    """A date in the form every RPOWER endpoint wants: YYYY-MM-DD, no time."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _biz_date(raw) -> Optional[str]:
    """The business date off a record, as YYYY-MM-DD.

    RPOWER returns dates as "2020-01-21T00:00:00" — a date with a zeroed time
    component, not a timestamp. Slicing is deliberate: parsing and
    re-formatting would invite a timezone conversion, and there is no
    timezone here to convert.
    """
    if not raw:
        return None
    return str(raw)[:10] or None


# ── bootstrap ───────────────────────────────────────────────────────────────

def list_stores(token: str) -> list:
    """Every store this token may read. RPOWER's documented first call."""
    body = _request(token, "store/get")
    return body if isinstance(body, list) else ([body] if body else [])


def test_token(token: str) -> dict:
    """Does this token work, and what does it see?

    Used by the admin connect screen before anything is saved, so a typo is
    caught at paste time rather than by a sync failing quietly at 5am.
    """
    try:
        stores = list_stores(token)
    except RPowerAuthError as e:
        return {"ok": False, "error": str(e)}
    except RPowerError as e:
        return {"ok": False, "error": f"Couldn't reach RPOWER: {e}"}
    if not stores:
        return {"ok": False,
                "error": "The token works but is not authorised for any store yet. "
                         "RPOWER has to grant it access to this customer."}
    return {
        "ok": True,
        "stores": [{
            "cg": s.get("cg"),
            "store_mid": s.get("mid"),
            "name": s.get("name"),
            "serial_number": s.get("serial_number"),
            "timezone": s.get("timezone"),
            "week_dow": s.get("week_dow"),
            "ot_dow": s.get("ot_dow"),
            "open_days": s.get("open_days"),
        } for s in stores],
    }


def bootstrap(restaurant_id: int, store_mid: str = None) -> dict:
    """Resolve and store cg + store_mid for a restaurant that has a token.

    RPOWER issues a token that may see several stores, so when more than one
    comes back the caller must choose — this refuses rather than picking the
    first, because silently binding a restaurant to the wrong store produces
    a module full of somebody else's numbers that looks entirely normal.

    Also carries across the settings RPOWER already knows and Cavnar was
    asking admins to type: the IANA timezone, and the day the payroll week
    starts on (`ot_dow`), which is what the labor module anchors overtime to.
    """
    from models import update_restaurant, get_restaurant
    c = _creds(restaurant_id)
    if not c["token"]:
        return {"ok": False, "error": "No RPOWER token on file for this restaurant."}

    probe = test_token(c["token"])
    if not probe["ok"]:
        update_restaurant(restaurant_id, {"rpower_sync_error": probe["error"]})
        return probe

    stores = probe["stores"]
    if store_mid:
        chosen = next((s for s in stores if str(s["store_mid"]) == str(store_mid)), None)
        if not chosen:
            return {"ok": False, "error": "That store isn't one this token can see.",
                    "stores": stores}
    elif len(stores) == 1:
        chosen = stores[0]
    else:
        return {"ok": False, "needs_choice": True, "stores": stores,
                "error": f"This token can see {len(stores)} stores — pick which one "
                         f"this restaurant is."}

    fields = {
        "rpower_cg": chosen["cg"],
        "rpower_store_mid": str(chosen["store_mid"]),
        "rpower_store_name": chosen.get("name"),
        "rpower_verified_at": datetime.utcnow().isoformat(timespec="seconds"),
        "rpower_sync_error": None,
    }
    # RPOWER's own timezone and payroll-week anchor, which are the same two
    # settings an admin was being asked to know. Only filled when Cavnar has
    # nothing already — an operator's explicit choice outranks the POS.
    r = get_restaurant(restaurant_id)
    if chosen.get("timezone") and not (r and r.timezone and r.timezone != "America/Chicago"):
        fields["timezone"] = chosen["timezone"]
    if chosen.get("ot_dow") is not None and r is not None and not getattr(r, "week_start_day", 0):
        # Two different conventions for the same fact, so this conversion is
        # spelled out rather than assumed: RPOWER's ot_dow is 1-based starting
        # Monday (1=Mon..7=Sun), while Cavnar's week_start_day is Python's
        # weekday() — 0-based, also starting Monday. Off by one, nothing more,
        # but silently off by one is a payroll week that starts on the wrong
        # day and an overtime calculation that splits in the wrong place.
        try:
            fields["week_start_day"] = (int(chosen["ot_dow"]) - 1) % 7
        except (TypeError, ValueError):
            pass
    update_restaurant(restaurant_id, fields)
    return {"ok": True, "store": chosen, "applied": sorted(fields)}


def _ctx(restaurant_id: int) -> tuple:
    """(token, base params) for a connected restaurant, or raise."""
    c = _creds(restaurant_id)
    if not (c["token"] and c["cg"] and c["store_mid"]):
        raise RPowerAuthError(
            "RPOWER is not fully connected — run bootstrap to resolve the store.")
    return c["token"], {"cg": c["cg"], "storemid": c["store_mid"]}


# ── sales type: what actually counts as revenue ─────────────────────────────

def sales_types(restaurant_id: int) -> dict:
    """Every sales type for this store, keyed by mid.

    This is the difference between net sales and a number that quietly
    includes comps, voids, gratuities and waste. RPOWER models each type with
    `impacts_sales` / `impacts_costs` plus explicit `type_comp`, `type_waste`,
    `type_void`-style flags, and a ticket line carries the type it was rung
    under. Summing sales without consulting this returns gross activity, and
    every food cost percentage computed against it is wrong by whatever the
    restaurant comps in a week.
    """
    token, base = _ctx(restaurant_id)
    rows = _paged(token, "salestype/getbycg", {"cg": base["cg"]})
    return {str(r.get("mid")): r for r in rows if r.get("mid")}


def _counts_as_revenue(row: dict, types: dict) -> bool:
    """Whether one ticketsales line belongs in net sales."""
    if row.get("voided"):
        return False
    st = types.get(str(row.get("slstype_mid")))
    if st is None:
        # An unknown sales type is not assumed to be revenue. RPOWER's own
        # data drives this decision and a missing lookup means we cannot make
        # it — counting it in would inflate sales, which is the direction that
        # makes food cost % look better than it is.
        return False
    if not st.get("impacts_sales"):
        return False
    for flag in ("type_comp", "type_waste", "type_error", "type_return",
                 "type_refund", "type_grat", "type_hidgrat", "type_housegrat",
                 "type_tax", "type_fee"):
        if st.get(flag):
            return False
    return True


# ── the reads pos.py and the modules need ───────────────────────────────────

def fetch_business_days(restaurant_id: int, start_date, end_date) -> dict:
    """{'YYYY-MM-DD': net_sales} over a business-date range.

    Matches toast.fetch_business_days' contract exactly so cogs.py and the
    labor normaliser can use either provider without knowing which.

    Net, not gross: every line is filtered through `_counts_as_revenue`.
    Built from `ticketsales` rather than `closeday` because closeday records
    the close event (from/thru date and shift) and not the money.
    """
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    out = {}
    for chunk_start, chunk_end in _chunk_range(start_date, end_date):
        rows = _paged(token, "ticketsales/getbybusinessdate", {
            **base, "startdate": _d(chunk_start), "enddate": _d(chunk_end),
            "sortorder": "date"})
        for row in rows:
            day = _biz_date(row.get("date"))
            if not day or not _counts_as_revenue(row, types):
                continue
            out[day] = round(out.get(day, 0.0) + float(row.get("sales") or 0), 2)
    return out


def _loss_kind(row: dict, types: dict):
    """comp | void | refund, or None for an ordinary sale."""
    if row.get("voided"):
        return "void"
    st = types.get(str(row.get("slstype_mid"))) or {}
    if st.get("type_comp"):
        return "comp"
    if st.get("type_refund") or st.get("type_return"):
        return "refund"
    return None


def fetch_loss_lines(restaurant_id: int, start_date, end_date) -> list:
    """Every comp, void and refund line over a business-date range.

    Fields come from RPOWER's documented ticketsales response: `voided`,
    `slstype_mid` (resolved through sales_types), `mgr_mid` (the manager who
    approved a comp or refund), `voidmgr_mid` and `voidrsn_mid` (who voided it
    and why), `shift`, `qty`, `price`, `regular_price`, `sales`.

    The APPROVER is the attribution, not the server. That is the loss-
    prevention convention and it is also the fair one: a comp is a manager's
    decision, and the data says whose.

    UNVERIFIED until the first live sync: whether a comp line's `sales` is
    zero, negative or the comped value. The amount below prefers
    regular_price x qty (what the item would have sold for) and falls back to
    |sales| — stated to the owner as "as reported by RPOWER".
    """
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    out = []
    for chunk_start, chunk_end in _chunk_range(start_date, end_date):
        rows = _paged(token, "ticketsales/getbybusinessdate", {
            **base, "startdate": _d(chunk_start), "enddate": _d(chunk_end),
            "sortorder": "date"})
        for row in rows:
            kind = _loss_kind(row, types)
            if not kind:
                continue
            qty = abs(float(row.get("qty") or 1) or 1)
            unit = row.get("regular_price")
            if unit in (None, "", 0):
                unit = row.get("price")
            try:
                amount = abs(float(unit)) * qty if unit not in (None, "") else abs(float(row.get("sales") or 0))
            except (TypeError, ValueError):
                amount = abs(float(row.get("sales") or 0))
            approver = row.get("voidmgr_mid") if kind == "void" else row.get("mgr_mid")
            out.append({"business_date": _biz_date(row.get("date")), "kind": kind,
                        "amount": round(amount, 2), "approver": str(approver) if approver else None,
                        "reason": row.get("voidrsn_mid"), "shift": row.get("shift")})
    return out


def fetch_order_selections(restaurant_id: int, business_date) -> list:
    """One business date's sold items, in toast.fetch_order_selections' shape:
    [{"item": {"guid": <menuitem_mid>}, "quantity": n}, ...].

    Shaped this way on purpose — inventory_ledger.compute_daily_depletion
    reads exactly this, and matching it means recipe-driven depletion, menu
    discovery and menu_item_sales all work for an RPOWER restaurant without a
    line of change in that module.

    Reads `ticketsales`, NOT `ticketitem`: RPOWER states plainly that ticket
    items are for reconstructing a receipt and must not be used for totals,
    and ticketitem's modifier rows would count a burger with four modifiers
    as five items sold.
    """
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    day = _d(business_date)
    rows = _paged(token, "ticketsales/getbybusinessdate", {
        **base, "startdate": day, "enddate": day, "sortorder": "date"})

    by_item = {}
    for row in rows:
        if row.get("voided"):
            continue
        st = types.get(str(row.get("slstype_mid")))
        # A comped or wasted plate still LEAVES THE KITCHEN — it depletes
        # stock even though it earns nothing. So depletion deliberately keeps
        # what `_counts_as_revenue` drops, and only skips lines that never
        # became food: voids, returns and refunds.
        if st and (st.get("type_return") or st.get("type_refund")):
            continue
        guid = row.get("menuitem_mid")
        if not guid:
            continue
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        by_item[guid] = by_item.get(guid, 0.0) + qty
    return [{"item": {"guid": g}, "quantity": q} for g, q in by_item.items()]


def fetch_menu_items(restaurant_id: int) -> list:
    """The menu, for discovery and costing.

    `last_cost` is RPOWER's own cost-per-item where a store maintains it —
    a theoretical plate cost with no recipe mapping required. It is commonly
    0 (RPOWER's own examples show 0), so it is passed through as-is and the
    caller decides whether it is usable rather than this module pretending
    an unpopulated field is a measurement.
    """
    token, base = _ctx(restaurant_id)
    rows = _paged(token, "menuitem/getbycg", {"cg": base["cg"], "sortorder": "name"})
    return [{
        "guid": r.get("mid"),
        "name": r.get("name"),
        "is_modifier": bool(r.get("is_mod")),
        "sales_category_mid": r.get("slscat_mid"),
        "sales_type_mid": r.get("slstype_mid"),
        "last_cost": r.get("last_cost"),
        "plu": r.get("plu") or None,
    } for r in rows if r.get("mid")]


def fetch_time_entries(restaurant_id: int, start_date, end_date) -> list:
    """Timeclock punches over a date range.

    RPOWER has already split regular, overtime and doubletime hours and pay,
    and carries the job name and payroll id — so unlike Toast, none of that
    has to be re-derived. `break_minutes` is reported so a caller can decide
    whether the restaurant's breaks are paid; nothing here assumes.
    """
    token, base = _ctx(restaurant_id)
    out = []
    for chunk_start, chunk_end in _chunk_range(start_date, end_date):
        out.extend(_paged(token, "timeclock/getbydaterange", {
            **base, "startdate": _d(chunk_start), "enddate": _d(chunk_end),
            "sortorder": "in_dttm"}))
    return out


def _chunk_range(start_date, end_date, max_days: int = MAX_RANGE_DAYS):
    """Split a date range into request-sized chunks.

    A year-long backfill in one call would be one enormous paged read against
    an API with no documented rate limit or timeout behaviour. Chunking keeps
    each request bounded and lets a partial failure cost one month rather
    than the whole history.
    """
    start = start_date if hasattr(start_date, "toordinal") else date.fromisoformat(_d(start_date))
    end = end_date if hasattr(end_date, "toordinal") else date.fromisoformat(_d(end_date))
    if end < start:
        return
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


# ── labor: timeclock -> the shifts CSV labor.py already reads ───────────────

_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _punch_dt(raw) -> Optional[datetime]:
    """One timeclock punch as a datetime. RPOWER sends local wall-clock with
    no offset ("2019-06-23T06:58:00"), which is what the CSV wants, so this
    parses without attaching or converting a timezone."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def normalise_entries(time_entries: list, sales_by_date: dict) -> list:
    """Timeclock punches into labor.py's CSV rows.

    Two things RPOWER gives us that Toast does not, and both are kept rather
    than recomputed:

    * Hours are already split into reg/ot/dt. labor.py derives overtime
      itself from a weekly total, which is a reasonable approximation when
      that is all you have — but RPOWER's figures come from the payroll
      engine that actually pays these people, and a derived number that
      disagrees with the paycheck is worse than no number. `actual_hours` is
      therefore the sum of all three, and the split travels in `notes` so it
      is visible rather than discarded.
    * `job_id` is the real job code, so `role` is what the restaurant calls
      it rather than something inferred from a name.

    `sales` is attached per day, not per shift — the same convention
    toast.normalise_entries uses, so labor.py's day aggregation is unchanged.
    """
    rows = []
    for e in time_entries or []:
        start = _punch_dt(e.get("in_dttm"))
        if not start:
            continue
        end = _punch_dt(e.get("out_dttm"))
        # A punch with no out time is a shift still running. It has no
        # duration to report and including it as a zero-hour shift would drag
        # every average down, so it is skipped rather than guessed at.
        if not end or end <= start:
            continue
        day = start.date().isoformat()
        reg = float(e.get("reg_hours") or 0)
        ot = float(e.get("ot_hours") or 0)
        dt_h = float(e.get("dt_hours") or 0)
        actual = round(reg + ot + dt_h, 3)
        if actual <= 0:
            # Fall back to the clock only when RPOWER reported no hours at
            # all — a punch pair with no payroll hours behind it is usually a
            # correction, and the wall-clock duration is the honest read.
            actual = round((end - start).total_seconds() / 3600.0, 3)
        notes = []
        if ot:
            notes.append(f"OT {ot:g}h")
        if dt_h:
            notes.append(f"DT {dt_h:g}h")
        if e.get("break_minutes"):
            notes.append(f"break {int(e['break_minutes'])}m")
        rows.append({
            "date": day,
            "day": _DOW_NAMES[start.weekday()],
            "employee": (e.get("payroll_id") or e.get("emp_mid") or "").strip(),
            "role": (e.get("job_id") or "").strip(),
            "shift_start": start.strftime("%H:%M"),
            "shift_end": end.strftime("%H:%M"),
            # RPOWER's timeclock is what was WORKED. It carries no schedule,
            # so scheduled_hours is left equal to actual rather than invented
            # — a fabricated variance is worse than none.
            "scheduled_hours": actual,
            "actual_hours": actual,
            "sales": sales_by_date.get(day, ""),
            "notes": ", ".join(notes),
        })
    rows.sort(key=lambda r: (r["date"], r["shift_start"]))
    return rows


def build_shifts_csv(restaurant_id: int, days: int = 60) -> Optional[str]:
    """The last `days` of labor, in the CSV shape labor.load_shifts expects."""
    import csv
    import io as _io

    end = date.today()
    start = end - timedelta(days=days)
    entries = fetch_time_entries(restaurant_id, start, end)
    sales = fetch_business_days(restaurant_id, start, end)
    rows = normalise_entries(entries, sales)
    if not rows:
        return ""
    fieldnames = ["date", "day", "employee", "role", "shift_start", "shift_end",
                  "scheduled_hours", "actual_hours", "sales", "notes"]
    buf = _io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def sync_to_db(restaurant_id: int) -> dict:
    """Nightly sync — the same contract toast.sync_to_db has, so pos.py can
    call either without knowing which."""
    from models import save_client_data, update_restaurant

    try:
        csv_str = build_shifts_csv(restaurant_id, days=60)
        if not csv_str:
            update_restaurant(restaurant_id, {
                "rpower_sync_error": "No timeclock data returned for the last 60 days"})
            return {"ok": False, "error": "No shift data returned from RPOWER"}

        rows = csv_str.count("\n") - 1
        save_client_data(restaurant_id, "shifts", csv_str, source="rpower")
        update_restaurant(restaurant_id, {
            "rpower_last_synced": datetime.utcnow().isoformat(timespec="seconds"),
            "rpower_sync_error": None})

        # Same archival step toast.sync_to_db does: without it the rolling
        # 60-day CSV is refreshed nightly and labor_daily_history — which is
        # what year-over-year and the trend charts actually read — never
        # accumulates for a POS-connected restaurant.
        try:
            from labor import analyse_shifts_for_restaurant
            from models import save_labor_daily_history, save_labor_snapshot
            analysis = analyse_shifts_for_restaurant(restaurant_id)
            save_labor_daily_history(restaurant_id, analysis.get("by_day", {}))
            # Same as the Toast sync: keep menu_items current so the dish a
            # post names, the count sheet and recipe drafts have RPOWER's
            # items to match against — not only when Food Cost is open.
            try:
                import inventory_ledger
                inventory_ledger.discover_menu_items(restaurant_id, days=2)
            except Exception as _menu_e:
                log.warning("[rpower sync] menu item discovery error for %s: %s", restaurant_id, _menu_e)
            dr = analysis.get("date_range", {})
            if dr.get("start") and dr.get("end"):
                save_labor_snapshot(restaurant_id, dr["start"], dr["end"],
                                    analysis["overall_labor_pct"],
                                    analysis["total_labor_cost"],
                                    analysis["total_sales"])
        except Exception as e:
            log.warning("[rpower] labor archive failed for %s: %s", restaurant_id, e)

        return {"ok": True, "rows": rows}
    except RPowerAuthError as e:
        update_restaurant(restaurant_id, {"rpower_sync_error": str(e)})
        return {"ok": False, "error": str(e), "auth": True}
    except Exception as e:
        update_restaurant(restaurant_id, {"rpower_sync_error": str(e)[:300]})
        return {"ok": False, "error": str(e)}


def get_connection_status(restaurant_id: int) -> dict:
    """What the account screen shows. Distinguishes the three real states:
    nothing on file, a token that has never been verified, and connected."""
    try:
        c = _creds(restaurant_id)
    except RPowerError as e:
        return {"connected": False, "state": "error", "error": str(e)}
    if not c["token"]:
        return {"connected": False, "state": "not_configured"}
    if not (c["cg"] and c["store_mid"]):
        return {"connected": False, "state": "needs_bootstrap",
                "message": "Token saved — choose which RPOWER store this is."}
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return {
        "connected": True, "state": "connected",
        "store_name": c["name"], "cg": c["cg"], "store_mid": c["store_mid"],
        "last_synced": getattr(r, "rpower_last_synced", None),
        "error": getattr(r, "rpower_sync_error", None),
    }


# ── write path: pushing a Cavnar schedule back into RPOWER ─────────────────

def push_labor_schedule(restaurant_id: int, shifts: list) -> dict:
    """Send a generated schedule to the store's POS.

    The only write in the Core API, and it is OFF by default: RPOWER states
    "Labor schedule push is not enabled by default. If you would like the
    ability to push labor schedules to stores, email a request." So a 401/403
    here means the feature is not enabled for this token, which is a
    different problem from a bad token and is reported as such.

    AS OF 18 Sep 2026 this is EXPECTED to fail: RPOWER confirmed our access is
    read-only, and said there is "a secondary way apart from the API to push
    schedules if we need to do that temporarily". So until the write scope is
    granted, weekly schedule delivery goes out by Cavnar's own email path
    (labor's publish_schedule, which emails each employee their own shifts)
    and this function stays unused rather than removed — the moment the scope
    is enabled it is the path, and the 401/403 message already tells an
    operator exactly what to ask for.

    `shifts` is Cavnar's own shape — [{employee_payroll_id, job, start, end}]
    — grouped here into RPOWER's per-employee structure. Times are local wall
    clock, matching what RPOWER documents and what the timeclock returns.
    """
    token, base = _ctx(restaurant_id)
    by_employee = {}
    skipped = []
    for s in shifts or []:
        pid = (s.get("employee_payroll_id") or s.get("payroll_id") or "").strip()
        job = (s.get("job") or s.get("role") or "").strip()
        start, end = s.get("start"), s.get("end")
        # An employee with no payroll id cannot be matched to anyone at the
        # store. Pushing them under a blank id would either fail the whole
        # batch or silently attach hours to nobody, so they are reported back
        # instead of dropped.
        if not (pid and job and start and end):
            skipped.append({"employee": s.get("employee") or pid or "(unnamed)",
                            "why": "missing payroll id, job, or times"})
            continue
        by_employee.setdefault(pid, []).append({
            "inTime": str(start)[:19], "jobCode": job, "outTime": str(end)[:19]})

    if not by_employee:
        return {"ok": False, "error": "No shifts had a payroll id and job to push.",
                "skipped": skipped}

    payload = {"storeMid": base["storemid"],
               "schedules": [{"payrollid": pid, "schedules": rows}
                             for pid, rows in by_employee.items()]}
    import requests
    try:
        resp = requests.post(
            f"{BASE_URL}/laborschedule/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=TIMEOUT_SECONDS)
    except Exception as e:
        return {"ok": False, "error": f"Couldn't reach RPOWER: {e}", "skipped": skipped}

    if resp.status_code in (401, 403):
        return {"ok": False, "skipped": skipped,
                "error": "RPOWER refused the schedule push. It is disabled by default — "
                         "email integrations@rpower.com to enable it for this token."}
    if not resp.ok:
        return {"ok": False, "skipped": skipped,
                "error": f"RPOWER returned HTTP {resp.status_code}: {resp.text[:200]}"}
    return {"ok": True, "employees": len(by_employee),
            "shifts": sum(len(v) for v in by_employee.values()), "skipped": skipped}
