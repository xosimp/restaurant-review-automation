"""
toast.py — Toast POS API client for Cavnar AI

Handles authentication (client_credentials), labor shift fetching,
and daily sales aggregation. All data is normalized into the same
dict-list format that labor.py already understands, so nothing in
the analysis engine changes.

Toast API base: https://ws-api.toasttab.com (production)
                https://ws-sandbox.toasttab.com (sandbox)
"""
import os, csv, io, requests
from datetime import datetime, timezone, timedelta, date
from typing import Optional

TOAST_BASE    = os.getenv("TOAST_API_BASE", "https://ws-api.toasttab.com")
TOAST_SANDBOX = "https://ws-sandbox.toasttab.com"

_DEMO_ID = "demo"  # a credential field equal to this triggers demo mode — on a demo restaurant only


# ── Demo mode ─────────────────────────────────────────────────────────────────

def demo_allowed(restaurant_id: int) -> bool:
    """Whether this restaurant may run on the synthetic "demo" Toast feed:
    only when it is flagged restaurants.is_demo=1. Any owner could type
    "demo" into the connect form and load 600 synthetic shifts and eight
    weeks of invented sales into a real restaurant, which then read "Toast
    synced" and fed the cross-restaurant benchmarks (CA3 F8)."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return bool(r and int(getattr(r, "is_demo", 0) or 0) == 1)
    except Exception:
        return False


def _is_demo(restaurant_id: int) -> bool:
    """True when a credential field is the literal string 'demo' AND the
    restaurant is flagged is_demo. On a real restaurant "demo" credentials
    are just wrong credentials: the sync calls Toast and fails honestly."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return bool(r and int(getattr(r, "is_demo", 0) or 0) == 1 and (
            r.toast_client_id == _DEMO_ID or
            r.toast_client_secret == _DEMO_ID or
            r.toast_restaurant_guid == _DEMO_ID
        ))
    except Exception:
        return False


def _demo_shifts_csv() -> str:
    """
    Generate a realistic 8-week CSV of shift + sales data for a busy
    Italian casual-dining restaurant. Used in demo mode
    so the full labor.py analysis pipeline can be exercised without real creds.
    """
    import random
    from datetime import date as _date, timedelta as _td

    random.seed(42)  # reproducible so the numbers don't change on re-sync

    staff = [
        ("Maria S.", "Server"),
        ("Jake T.", "Server"),
        ("Priya K.", "Server"),
        ("Tom R.", "Bartender"),
        ("Sofia D.", "Bartender"),
        ("Carlos M.", "Cook"),
        ("Amy L.", "Cook"),
        ("Luis G.", "Cook"),
        ("James H.", "Host"),
    ]

    # Typical daily sales by day of week (Italian casual, dinner-focused)
    _base_sales = {
        0: 4800,   # Monday
        1: 5200,   # Tuesday
        2: 5600,   # Wednesday
        3: 6200,   # Thursday
        4: 8800,   # Friday
        5: 9600,   # Saturday
        6: 6800,   # Sunday
    }

    # Typical shift windows per role
    _shifts = {
        "Server":    [("11:00", "17:00", 6), ("17:00", "23:00", 6)],
        "Bartender": [("16:00", "24:00", 8)],
        "Cook":      [("10:00", "18:00", 8), ("16:00", "24:00", 8)],
        "Host":      [("17:00", "22:00", 5)],
    }

    fieldnames = ["date","day","employee","role","shift_start","shift_end",
                  "scheduled_hours","actual_hours","sales","notes"]

    rows = []
    today = _date.today()
    start = today - _td(days=56)  # 8 weeks back

    _dow = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    for i in range(56):
        d     = start + _td(days=i)
        dow   = d.weekday()
        sales = _base_sales[dow] + random.randint(-400, 600)
        date_str = d.strftime("%Y-%m-%d")
        day_name = _dow[dow]

        for emp, role in staff:
            windows = _shifts.get(role, [])
            # Servers: both shifts on Fri/Sat, one shift other days
            if role == "Server":
                chosen = windows if dow in (4, 5) else [windows[0]]
            # Cooks: both shifts Thu–Sat
            elif role == "Cook" and emp in ("Carlos M.", "Amy L."):
                chosen = windows if dow in (3, 4, 5) else [windows[0]]
            else:
                chosen = [windows[0]] if windows else []

            for (s_start, s_end, sched_h) in chosen:
                actual_h = round(sched_h + random.uniform(-0.4, 0.5), 1)
                rows.append({
                    "date":            date_str,
                    "day":             day_name,
                    "employee":        emp,
                    "role":            role,
                    "shift_start":     s_start,
                    "shift_end":       s_end,
                    "scheduled_hours": sched_h,
                    "actual_hours":    actual_h,
                    "sales":           sales,
                    "notes":           "Toast POS (demo)",
                })

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ── Authentication ─────────────────────────────────────────────────────────────

def _get_raw_token(client_id: str, client_secret: str, base_url: str = TOAST_BASE) -> dict:
    """Exchange client credentials for an access token. Returns the full response dict."""
    resp = requests.post(
        f"{base_url}/authentication/v1/authentication/login",
        json={
            "clientId":      client_id,
            "clientSecret":  client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_toast_token(restaurant_id: int) -> str:
    """
    Return a valid access token for the restaurant.
    Refreshes automatically if expired (tokens last 24h).
    Raises RuntimeError if Toast is not connected.
    """
    if _is_demo(restaurant_id):
        return "demo-token"

    from models import get_restaurant, update_restaurant

    r = get_restaurant(restaurant_id)
    if not r or not r.toast_client_id or not r.toast_client_secret:
        raise RuntimeError("Toast not connected — credentials missing")

    # Use cached token if still valid (5-minute buffer)
    if r.toast_access_token and r.toast_token_expires:
        try:
            expires = datetime.fromisoformat(r.toast_token_expires)
            if datetime.now(timezone.utc) < expires - timedelta(minutes=5):
                return r.toast_access_token
        except Exception:
            pass

    # Fetch a fresh token
    base = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    data = _get_raw_token(r.toast_client_id, r.toast_client_secret, base)

    token      = data["token"]["accessToken"]
    expires_in = data["token"].get("expiresIn", 86400)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    update_restaurant(restaurant_id, {
        "toast_access_token":  token,
        "toast_token_expires": expires_at,
    })
    return token


def _http(fn, url, **kwargs):
    """One provider call through pos.http_call: retried on a timeout, a
    dropped connection, 429 and 5xx (Retry-After honoured), never on 401 or
    403 (DH2-5). Every caller still names its timeout."""
    import pos
    return pos.http_call(fn, url, **kwargs)


# ── Data fetching ──────────────────────────────────────────────────────────────

def _headers(token: str, restaurant_guid: str) -> dict:
    return {
        "Authorization":               f"Bearer {token}",
        "Toast-Restaurant-External-ID": restaurant_guid,
        "Content-Type":                "application/json",
    }


# Pages read before a fetch gives up. 200 x 100 = 20,000 time entries (a
# 500-person roster's 60 days is ~21,000 at the extreme; ordinary ones are a
# few thousand) or 20,000 orders in one business day. Hitting it RAISES
# ToastTruncated: the old 20-page cap stopped silently at 2,000 entries, and
# the sync saved that fraction over a complete CSV and reported ok.
PAGE_LIMIT = 200


class ToastTruncated(RuntimeError):
    """Toast had more pages than PAGE_LIMIT; the data read is incomplete."""


def fetch_time_entries(restaurant_id: int, start_date: date, end_date: date,
                       start_at: datetime = None, end_at: datetime = None) -> list:
    """
    Pull clock-in/clock-out time entries from Toast Labor API with pagination.
    Toast returns up to 100 entries per page; we loop until no nextPageToken.
    Returns a flat list of all raw Toast timeEntry dicts.

    The window is whole UTC calendar days from start_date to end_date, or
    exactly [start_at, end_at] when both (aware datetimes) are given — the
    live clock-in feed asks for one restaurant-local business day that way
    (DH2-8).
    """
    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE

    # Toast expects RFC 3339 with +00:00 offset
    if start_at is not None and end_at is not None:
        start_iso = start_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        end_iso = end_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    else:
        start_iso = datetime.combine(start_date, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        end_iso   = datetime.combine(end_date, datetime.max.time().replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

    all_entries = []
    page_token  = None

    for _ in range(PAGE_LIMIT):
        params = {"startDate": start_iso, "endDate": end_iso, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        resp = _http(requests.get, 
            f"{base}/labor/v1/timeEntries",
            headers=_headers(token, r.toast_restaurant_guid),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()

        # Response may be a plain list or a paged object {timeEntries: [...], nextPageToken: ...}
        if isinstance(body, list):
            all_entries.extend(body)
            break
        else:
            all_entries.extend(body.get("timeEntries", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    else:
        raise ToastTruncated(f"Toast returned more than {PAGE_LIMIT * 100:,} time entries for "
                             f"{start_date.isoformat()} to {end_date.isoformat()}; nothing was saved from this read")

    return all_entries


def fetch_business_days(restaurant_id: int, start_date: date, end_date: date) -> dict:
    """
    Pull daily net sales from Toast's Business Day endpoint.
    Returns {YYYY-MM-DD: net_sales_float}.
    """
    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE

    # Toast businessDate format is YYYYMMDD
    start_str = start_date.strftime("%Y%m%d")
    end_str   = end_date.strftime("%Y%m%d")

    resp = _http(requests.get, 
        f"{base}/businessDay/v1/businessDays",
        headers=_headers(token, r.toast_restaurant_guid),
        params={"start": start_str, "end": end_str},
        timeout=30,
    )
    resp.raise_for_status()

    sales_by_date = {}
    for day in resp.json():
        # businessDate is YYYYMMDD int or string
        bd = str(day.get("businessDate", ""))
        if len(bd) == 8:
            iso = f"{bd[:4]}-{bd[4:6]}-{bd[6:8]}"
        else:
            iso = bd
        # A day with no netSales field is UNKNOWN — None, stored blank —
        # never $0 (pos.py's sales contract; DH2-6). `or 0` turned a missing
        # field into a night with no sales.
        net = day.get("netSales")
        sales_by_date[iso] = float(net) if net is not None else None
    return sales_by_date


def _demo_order_selections(business_date: date) -> list:
    """Canned selections for demo-mode restaurants, mirroring
    _demo_shifts_csv()'s role for build_shifts_csv — so the nightly
    depletion job never throws on a restaurant with no real Toast
    credentials. A small, fixed menu so demo recipe setup has something
    real to map."""
    import random
    random.seed(business_date.toordinal())  # reproducible per day
    demo_menu = [
        ("11111111-demo-caesar",   "Caesar Salad",   (8, 22)),
        ("22222222-demo-salmon",   "Grilled Salmon",  (10, 26)),
        ("33333333-demo-rigatoni", "Rigatoni Bolognese", (14, 30)),
        ("44444444-demo-margherita", "Margherita Pizza", (12, 28)),
    ]
    selections = []
    for guid, name, (lo, hi) in demo_menu:
        selections.append({
            "item": {"guid": guid, "externalId": guid},
            "displayName": name,
            "quantity": random.randint(lo, hi),
            "voided": False,
        })
    return selections


def fetch_order_selections(restaurant_id: int, business_date: date) -> list:
    """
    Pull every order-line-item ("selection") for one Toast business date via
    GET /orders/v2/ordersBulk?businessDate=YYYYMMDD, flattening
    orders -> checks[] -> selections[]. Each returned dict carries at least
    item.guid, displayName, and quantity — enough to match against
    recipe_ingredients (see inventory_ledger.py) and compute ingredient
    depletion from real sales.

    Paginated by page/pageSize (max 100) — NOT the pageToken style
    fetch_time_entries uses, ordersBulk is a different endpoint shape.
    Voided/refunded selections are excluded so cancelled orders don't
    deplete stock that was never actually used.

    Callers should get business_date from fetch_business_days()'s own
    returned dates, not local date math — Toast's business-day boundary
    isn't calendar midnight, so "today - 1 day" can silently point at the
    wrong day around close time.
    """
    if _is_demo(restaurant_id):
        return _demo_order_selections(business_date)

    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    business_date_str = business_date.strftime("%Y%m%d")

    all_selections = []
    page = 1
    for _ in range(PAGE_LIMIT):
        resp = _http(requests.get, 
            f"{base}/orders/v2/ordersBulk",
            headers=_headers(token, r.toast_restaurant_guid),
            params={"businessDate": business_date_str, "page": page, "pageSize": 100},
            timeout=30,
        )
        resp.raise_for_status()
        orders = resp.json() or []
        if not orders:
            break

        for order in orders:
            for check in (order.get("checks") or []):
                for sel in (check.get("selections") or []):
                    # `voided` is documented; `refundStatus` is a best-effort
                    # guess at the refund field name pending verification
                    # against a real sandbox response — harmless if wrong
                    # (just never matches), but confirm before relying on it.
                    if sel.get("voided") or sel.get("refundStatus") not in (None, "NONE"):
                        continue
                    all_selections.append(sel)

        if len(orders) < 100:
            break
        page += 1
    else:
        # A partial day would under-deplete every ingredient; refuse it.
        raise ToastTruncated(f"Toast returned more than {PAGE_LIMIT * 100:,} orders for {business_date_str}")

    return all_selections


def _demo_order_customers(business_date: date) -> list:
    """Two identified guests per demo day — enough to exercise the opt-in
    invite job end to end without a real Toast account."""
    stamp = business_date.strftime("%Y%m%d")
    return [
        {"order_guid": f"demo-order-{stamp}-1", "name": "Demo Guest One",
         "phone": "+15550000001", "email": "guest1@demo.test"},
        {"order_guid": f"demo-order-{stamp}-2", "name": "Demo Guest Two",
         "phone": "+15550000002", "email": "guest2@demo.test"},
    ]


def fetch_order_customers(restaurant_id: int, business_date: date) -> list:
    """Guests Toast actually identified on one business date.

    Same ordersBulk endpoint and pagination as fetch_order_selections, just
    reading checks[].customer instead of checks[].selections. Returns
    [{order_guid, name, phone, email}] for checks that carry a customer with
    a phone number.

    Most dine-in covers will NOT appear here: Toast only has a customer on
    a check when one was actually captured (online ordering, loyalty, or a
    server entering it). That is expected, not a bug — this is a partial
    signal by nature, which is why the caller treats every number it
    returns as unconsented until the guest opts in themselves.

    The customer object's exact shape is documented by Toast but has not
    been verified against a live response from this account, so every field
    read here is defensive.
    """
    if _is_demo(restaurant_id):
        return _demo_order_customers(business_date)

    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    business_date_str = business_date.strftime("%Y%m%d")

    customers = []
    seen = set()
    page = 1
    for _ in range(20):
        resp = _http(requests.get, 
            f"{base}/orders/v2/ordersBulk",
            headers=_headers(token, r.toast_restaurant_guid),
            params={"businessDate": business_date_str, "page": page, "pageSize": 100},
            timeout=30,
        )
        resp.raise_for_status()
        orders = resp.json() or []
        if not orders:
            break

        for order in orders:
            if order.get("voided") or order.get("deleted"):
                continue
            order_guid = order.get("guid") or ""
            for check in (order.get("checks") or []):
                cust = check.get("customer") or {}
                phone = (cust.get("phone") or "").strip()
                if not phone:
                    continue
                name = " ".join(p for p in [(cust.get("firstName") or "").strip(),
                                            (cust.get("lastName") or "").strip()] if p)
                key = (order_guid, phone)
                if key in seen:
                    continue
                seen.add(key)
                customers.append({
                    "order_guid": order_guid,
                    "name": name,
                    "phone": phone,
                    "email": (cust.get("email") or "").strip(),
                })

        if len(orders) < 100:
            break
        page += 1

    return customers


# ── The nightly DSR: one business day's sales ─────────────────────────────────

UNCATEGORISED = "Uncategorized"          # a selection with no sales category
UNRESOLVED_CATEGORY = "Unknown category" # one whose category name Toast wouldn't give us


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _discount_total(obj) -> float:
    return sum(_num(d.get("discountAmount")) for d in (obj.get("appliedDiscounts") or []) if isinstance(d, dict))


def _sales_category_names(restaurant_id, token, guid, base) -> dict:
    """{sales category guid: name} from Toast's configuration API. ordersBulk
    names a selection's category by reference only. Best effort: a failure
    leaves the names unresolved (the DSR shows them as unmapped) rather than
    failing the whole day's figures."""
    try:
        resp = _http(requests.get, f"{base}/config/v2/salesCategories", headers=_headers(token, guid), timeout=30)
        resp.raise_for_status()
        body = resp.json() or []
        return {c.get("guid"): (c.get("name") or "").strip() for c in body if isinstance(c, dict) and c.get("guid")}
    except Exception as e:
        print(f"[toast] sales category names unavailable for {restaurant_id}: {e}")
        return {}


def fetch_day_sales(restaurant_id: int, business_date: date) -> dict:
    """One business date's sales in pos.fetch_day_sales' PARTS shape — pos
    nets them by pos.NET_DEDUCTIONS.

    GET /orders/v2/ordersBulk?businessDate=YYYYMMDD, paged like
    fetch_order_selections. Orders and checks that are voided or deleted are
    not sales; their selections count as voids.

      net (Toast's own)  check.amount — after discounts, before tax
      discounts          check.appliedDiscounts + each selection's
                         appliedDiscounts (discountAmount)
      gross              amount + discounts — before any discount
      comps              None: Toast rings a comp as a discount, so it is
                         already inside discounts and cannot be separated
      tax                check.taxAmount
      by_department      selection.salesCategory (named through the config
                         API), from selection.price plus its own discounts;
                         check-level discounts are not on any selection, so
                         the departments can sum to less than net — the DSR
                         reports that difference as unallocated
      by_hour            the order's openedDate, in the restaurant's zone
      items              selection displayName, quantity and price
      voids              voided selections' preDiscountPrice (else price)
      refunds            selection.refundDetails.refundAmount

    Field names are Toast's documented Orders API; the salesCategory
    reference shape and refundDetails are not yet seen in a live response
    from this account, so every read is defensive.
    """
    if _is_demo(restaurant_id):
        raise NotImplementedError("Toast demo mode has no real day's sales to report")

    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    tz    = _tz_for(restaurant_id)
    business_date_str = business_date.strftime("%Y%m%d")

    orders = []
    page = 1
    for _ in range(PAGE_LIMIT):
        resp = _http(requests.get, 
            f"{base}/orders/v2/ordersBulk",
            headers=_headers(token, r.toast_restaurant_guid),
            params={"businessDate": business_date_str, "page": page, "pageSize": 100},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    else:
        # A partial day would under-report the night; refuse it.
        raise ToastTruncated(f"Toast returned more than {PAGE_LIMIT * 100:,} orders for {business_date_str}")

    names = None

    def category(sel):
        nonlocal names
        ref = sel.get("salesCategory") or {}
        if not isinstance(ref, dict) or not (ref.get("guid") or ref.get("name")):
            return UNCATEGORISED
        if (ref.get("name") or "").strip():
            return ref["name"].strip()
        if names is None:
            names = _sales_category_names(restaurant_id, token, r.toast_restaurant_guid, base)
        return names.get(ref.get("guid")) or UNRESOLVED_CATEGORY

    def parts():
        return {"gross": 0.0, "discounts": 0.0, "comps": None}

    total = parts()
    tax = voids = refunds = service = 0.0
    guests, transactions = 0, 0
    by_dep, by_hour, items = {}, {}, {}
    for order in orders:
        dead_order = bool(order.get("voided") or order.get("deleted"))
        opened = _parse_toast_time(order.get("openedDate"))
        hour = opened.astimezone(tz).strftime("%H") if opened and opened.tzinfo else (
            opened.strftime("%H") if opened else None)
        counted_order = False
        for check in (order.get("checks") or []):
            dead = dead_order or bool(check.get("voided") or check.get("deleted"))
            live_sels = []
            for sel in (check.get("selections") or []):
                if dead or sel.get("voided"):
                    voids += _num(sel.get("preDiscountPrice")) or _num(sel.get("price"))
                    continue
                live_sels.append(sel)
                refunds += _num((sel.get("refundDetails") or {}).get("refundAmount"))
            if dead:
                continue
            amount = _num(check.get("amount"))
            disc = _discount_total(check) + sum(_discount_total(s) for s in live_sels)
            tax += _num(check.get("taxAmount"))
            service += sum(_num(c.get("chargeAmount")) for c in (check.get("appliedServiceCharges") or [])
                           if isinstance(c, dict))
            if live_sels:
                transactions += 1
                counted_order = True
            delta = {"gross": amount + disc, "discounts": disc}
            for b in ([total] + ([by_hour.setdefault(hour, parts())] if hour else [])):
                b["gross"] += delta["gross"]
                b["discounts"] += delta["discounts"]
            for sel in live_sels:
                dep = category(sel)
                sel_disc = _discount_total(sel)
                price = _num(sel.get("price"))
                d = by_dep.setdefault(dep, parts())
                d["gross"] += price + sel_disc
                d["discounts"] += sel_disc
                name = (sel.get("displayName") or "").strip() or "Unknown item"
                key = ((sel.get("item") or {}).get("guid") or name, dep)
                it = items.setdefault(key, {"name": name, "department": dep, "qty": 0.0, "parts": parts(),
                                            "guid": (sel.get("item") or {}).get("guid")})
                it["qty"] += _num(sel.get("quantity"))
                it["parts"]["gross"] += price + sel_disc
                it["parts"]["discounts"] += sel_disc
        if counted_order:
            guests += int(_num(order.get("numberOfGuests")))
    return {
        "gross": total["gross"], "discounts": total["discounts"], "comps": None,
        "voids": round(voids, 2), "refunds": round(refunds, 2), "tax": round(tax, 2),
        "transactions": transactions, "guests": guests or None,
        "by_department": by_dep, "by_hour": by_hour,
        "items": [it for it in items.values() if it["qty"] > 0],
        "source_checks": {"orders": len(orders), "service_charges": round(service, 2)},
    }


# ── Data normalisation ─────────────────────────────────────────────────────────

_DOW = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

def _parse_toast_time(ts: str) -> Optional[datetime]:
    """Parse a Toast ISO timestamp to datetime, ignoring timezone for hour extraction."""
    if not ts:
        return None
    try:
        # Toast returns e.g. "2026-06-16T10:30:00.000+0000"
        return datetime.fromisoformat(ts.replace("+0000", "+00:00").replace("Z", "+00:00"))
    except Exception:
        return None


def _tz_for(restaurant_id):
    from time_utils import restaurant_tz
    try:
        from models import get_restaurant
        return restaurant_tz(get_restaurant(restaurant_id))
    except Exception:
        return restaurant_tz(None)


def _toast_name(emp: dict) -> str:
    """One name per Toast employee, the same in shift history and in the
    live clock-in feed. History used to abbreviate to "Maria G." while the
    live feed said "Maria Garcia", so no clock-in ever matched a scheduled
    name — and two Maria G.s were merged into one person."""
    first = (emp.get("firstName") or "").strip()
    last = (emp.get("lastName") or "").strip()
    return f"{first} {last}".strip()


# ── Who each time entry belongs to (DH2-7) ──────────────────────────────────
#
# Toast's Labor API time entries identify the person and the job by
# reference — employeeReference.guid, jobReference.guid — and normalise_entries
# read expanded employee/jobReference names only. With references alone every
# shift became "Unknown" / "Staff": one merged employee, phantom 1.5x overtime,
# and an empty clock-in feed. So names are resolved through the employees and
# jobs endpoints (once per sync, cached for the pull), and a pull where more
# than UNRESOLVED_MAX_SHARE of the entries still have no person FAILS rather
# than saving "Unknown".
# UNVERIFIED against a live Toast response: whether the entries carry
# references only, or expanded names as the test fixtures assume. Confirm on
# the first live Toast labor pull before trusting either path.
EMPLOYEE_RESOLUTION_UNVERIFIED = True
UNRESOLVED_MAX_SHARE = 0.05


class ToastUnresolved(RuntimeError):
    """Too many time entries could not be matched to a Toast employee."""


def _fetch_people(restaurant_id: int, cache: dict) -> dict:
    """{"employees": {guid: employee dict}, "jobs": {guid: title}} — read once
    per `cache` (one sync or one clock-in read)."""
    if "employees" in cache:
        return cache
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    emps, jobs = {}, {}
    resp = _http(requests.get, f"{base}/labor/v1/employees", headers=_headers(token, r.toast_restaurant_guid),
                 timeout=30)
    resp.raise_for_status()
    for e in resp.json() or []:
        if isinstance(e, dict) and e.get("guid"):
            emps[e["guid"]] = e
    resp = _http(requests.get, f"{base}/labor/v1/jobs", headers=_headers(token, r.toast_restaurant_guid),
                 timeout=30)
    resp.raise_for_status()
    for j in resp.json() or []:
        if isinstance(j, dict) and j.get("guid"):
            jobs[j["guid"]] = j.get("title") or j.get("name")
    cache.update(employees=emps, jobs=jobs)
    return cache


def resolve_people(restaurant_id: int, entries: list, cache: dict = None) -> int:
    """Give every entry that carries only an employeeReference / jobReference
    GUID its person and job title, in place. Returns how many entries still
    have no person. Calls Toast only when some entry needs it."""
    cache = {} if cache is None else cache
    needs = [e for e in entries or []
             if not _toast_name(e.get("employee") or {}) and (e.get("employeeReference") or {}).get("guid")
             or (not (e.get("jobReference") or {}).get("name") and (e.get("jobReference") or {}).get("guid"))]
    if needs:
        _fetch_people(restaurant_id, cache)
    unresolved = 0
    for e in entries or []:
        if not _toast_name(e.get("employee") or {}):
            guid = (e.get("employeeReference") or {}).get("guid")
            emp = (cache.get("employees") or {}).get(guid) if guid else None
            if emp and _toast_name(emp):
                e["employee"] = emp
            else:
                unresolved += 1
        jr = e.get("jobReference") or {}
        if not jr.get("name") and jr.get("guid"):
            title = (cache.get("jobs") or {}).get(jr["guid"])
            if title:
                e["jobReference"] = {**jr, "name": title}
    return unresolved


def _require_resolved(restaurant_id: int, entries: list, cache: dict = None) -> list:
    """resolve_people, then fail when more than UNRESOLVED_MAX_SHARE of the
    entries have no person: saved as "Unknown" they merge into one employee
    with false overtime (DH2-7)."""
    n = len(entries or [])
    if not n:
        return entries or []
    unresolved = resolve_people(restaurant_id, entries, cache)
    if unresolved and unresolved / float(n) > UNRESOLVED_MAX_SHARE:
        raise ToastUnresolved(f"{unresolved} of {n} Toast time entries could not be matched to an employee; "
                              "nothing was saved from this read")
    return entries


def normalise_entries(time_entries: list, sales_by_date: dict, tz=None) -> list:
    """
    Convert raw Toast timeEntry objects into dicts matching the CSV schema
    that labor.py understands:
      date, day, employee, role, shift_start, shift_end,
      scheduled_hours, actual_hours, sales, notes
    """
    rows = []
    for entry in time_entries:
        try:
            employee = _toast_name(entry.get("employee") or {}) or "Unknown"
            # The employee's Toast GUID (memory audit 9/29/26, identity): the
            # id the person keeps however their name is spelled.
            emp_guid = (str((entry.get("employee") or {}).get("guid") or "").strip()
                        or str((entry.get("employeeReference") or {}).get("guid") or "").strip())

            # Role / job
            job_ref = entry.get("jobReference", {}) or {}
            role    = job_ref.get("name", "Staff")

            # Times
            in_dt  = _parse_toast_time(entry.get("inDate"))
            out_dt = _parse_toast_time(entry.get("outDate"))
            if not in_dt:
                continue  # clock-out only or corrupt entry

            # Toast's timestamps are UTC. A 7:30pm CDT clock-in is 00:30 UTC
            # the next day, and dating it by the UTC clock put evening labor
            # on tomorrow's sales and a "00:30" start on the schedule. Local
            # time first; Toast's own businessDate wins when it is present.
            if tz is not None:
                in_dt = in_dt.astimezone(tz)
                out_dt = out_dt.astimezone(tz) if out_dt else None
            entry_date = in_dt.date()
            _bd = str(entry.get("businessDate") or "").strip()
            if len(_bd) == 8 and _bd.isdigit():
                try:
                    entry_date = date(int(_bd[:4]), int(_bd[4:6]), int(_bd[6:]))
                except ValueError:
                    pass
            date_str   = entry_date.strftime("%Y-%m-%d")
            day_name   = _DOW[entry_date.weekday()]

            # Hours — Toast provides paidMinutes when available
            paid_minutes = entry.get("paidMinutes")
            if paid_minutes is not None:
                actual_hours = round(float(paid_minutes) / 60, 2)
            elif out_dt:
                delta = (out_dt - in_dt).total_seconds() / 3600
                actual_hours = round(max(0, delta), 2)
            else:
                # Still clocked in: the hours are not known yet. 0 read as a
                # zero-hour shift, and reliability counted it as a no-show.
                actual_hours = ""

            # Scheduled hours — use declared schedule if present, else actual
            # (and say which: a copied actual is no schedule, and no
            # attendance reader may read a no-show out of it).
            sched_in  = _parse_toast_time(entry.get("scheduledInDate"))
            sched_out = _parse_toast_time(entry.get("scheduledOutDate"))
            if sched_in and sched_out:
                delta_s = (sched_out - sched_in).total_seconds() / 3600
                scheduled_hours = round(max(0, delta_s), 2)
                schedule_known = "1"
            else:
                scheduled_hours = actual_hours
                schedule_known = "0"

            shift_start = in_dt.strftime("%H:%M") if in_dt else ""
            shift_end   = out_dt.strftime("%H:%M") if out_dt else ""

            # Blank, never 0, for a day Toast returned no sales figure
            # (pos.py's sales contract; CA3 F3).
            sales = sales_by_date.get(date_str, "")

            rows.append({
                "date":             date_str,
                "day":              day_name,
                "employee":         employee,
                "role":             role,
                "shift_start":      shift_start,
                "shift_end":        shift_end,
                "scheduled_hours":  scheduled_hours,
                "actual_hours":     actual_hours,
                "sales":            sales,
                "notes":            "Toast POS",
                "employee_ext_id":  emp_guid,
                "employee_named":   "1" if employee != "Unknown" else "0",
                "schedule_known":   schedule_known,
            })
        except Exception as e:
            print(f"[toast] normalise_entries: skipping entry — {e}")
            continue
    return rows


def build_shifts_csv(restaurant_id: int, days: int = 60) -> str:
    """
    Fetch the last `days` days of Toast data and return a CSV string
    in the same format that labor.py's load_shifts() expects.
    """
    if _is_demo(restaurant_id):
        return _demo_shifts_csv()

    end   = date.today()
    start = end - timedelta(days=days)

    time_entries = _require_resolved(restaurant_id, fetch_time_entries(restaurant_id, start, end))
    sales        = fetch_business_days(restaurant_id, start, end)
    rows         = normalise_entries(time_entries, sales, tz=_tz_for(restaurant_id))

    if not rows:
        return ""

    fieldnames = ["date","day","employee","role","shift_start","shift_end",
                  "scheduled_hours","actual_hours","sales","notes",
                  "employee_ext_id","employee_named","schedule_known"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ── DB sync ────────────────────────────────────────────────────────────────────

def sync_to_db(restaurant_id: int) -> dict:
    """
    Pull Toast data for the last 60 days and write it into client_data.shifts_csv,
    exactly as if the client had uploaded a CSV manually.
    Returns {"ok": True, "rows": n} or {"ok": False, "error": "..."}.
    """
    from models import save_client_data, update_restaurant

    try:
        csv_str = build_shifts_csv(restaurant_id, days=60)
        if not csv_str:
            # An empty pull is a failed sync, written like RPOWER's so the
            # freshness registry and pos_health see it (re-audit B3#15).
            update_restaurant(restaurant_id, {
                "toast_sync_error": "No shift data returned for the last 60 days"})
            return {"ok": False, "error": "No shift data returned from Toast"}

        import pos
        row_count = pos.save_synced_shifts(restaurant_id, csv_str, "toast")
        update_restaurant(restaurant_id, {
            "toast_last_synced": datetime.now(timezone.utc).isoformat(),
            "toast_sync_error":  None,
        })

        # Keep the food-cost recipe editor's menu item list current on every
        # sync (manual "Sync now" or nightly) — new dishes get picked up
        # automatically; only mapping a new item's recipe stays a manual,
        # one-time-per-item step (see inventory_ledger.discover_menu_items).
        try:
            import inventory_ledger
            inventory_ledger.discover_menu_items(restaurant_id)
        except Exception as _menu_e:
            print(f"[toast sync] menu item discovery error: {_menu_e}")

        return {"ok": True, "rows": max(0, row_count)}

    except Exception as e:
        err = str(e)
        try:
            update_restaurant(restaurant_id, {"toast_sync_error": err[:500]})
        except Exception:
            pass
        return {"ok": False, "error": err}


# ── Connection helpers ─────────────────────────────────────────────────────────

def is_connected(restaurant_id: int) -> bool:
    """True if Toast credentials are stored for this restaurant."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return bool(r and r.toast_restaurant_guid and r.toast_client_id and r.toast_client_secret)


def get_connection_status(restaurant_id: int) -> dict:
    """
    Return a status dict for the admin UI:
      connected, last_synced, sync_error, restaurant_guid (masked)
    """
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r:
        return {"connected": False}

    connected = bool(r.toast_restaurant_guid and r.toast_client_id and r.toast_client_secret)
    guid      = r.toast_restaurant_guid or ""
    masked    = (guid[:8] + "****" + guid[-4:]) if len(guid) > 12 else (guid or "—")

    return {
        "connected":    connected,
        "guid_masked":  masked,
        "last_synced":  getattr(r, "toast_last_synced", None),
        "sync_error":   getattr(r, "toast_sync_error", None),
    }


def test_credentials(client_id: str, client_secret: str, restaurant_guid: str) -> dict:
    """
    Validate credentials by fetching a token and hitting a lightweight endpoint.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    Pass "demo" for all three fields to enter demo mode without hitting the API.
    The result then carries demo=True, and every caller refuses it unless the
    restaurant is flagged is_demo (demo_allowed) — CA3 F8.
    """
    if client_id == _DEMO_ID or client_secret == _DEMO_ID or restaurant_guid == _DEMO_ID:
        return {"ok": True, "demo": True}

    base = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE
    try:
        data  = _get_raw_token(client_id, client_secret, base)
        token = data["token"]["accessToken"]

        # Hit the business day endpoint for today only — cheap, confirms GUID works
        today = date.today().strftime("%Y%m%d")
        resp  = requests.get(
            f"{base}/businessDay/v1/businessDays",
            headers={
                "Authorization":               f"Bearer {token}",
                "Toast-Restaurant-External-ID": restaurant_guid,
            },
            params={"start": today, "end": today},
            timeout=15,
        )
        if resp.status_code in (200, 404):
            return {"ok": True}
        return {"ok": False, "error": f"API returned {resp.status_code}: {resp.text[:200]}"}
    except requests.HTTPError as e:
        return {"ok": False, "error": f"Auth failed ({e.response.status_code}) — check client ID and secret"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_sales_today(restaurant_id: int, business_date: date) -> float:
    """Net sales so far today. Toast's businessDay figure updates through
    service, which is what makes a mid-afternoon read possible at all."""
    sales = fetch_business_days(restaurant_id, business_date, business_date) or {}
    value = sales.get(business_date.isoformat())
    if value is None:
        raise KeyError("Toast returned no business day for today")
    return float(value)


def fetch_clock_ins_today(restaurant_id: int, business_date: date) -> list:
    """Everyone clocked in today: [{"employee", "role", "clocked_in_at"}].

    Names come from the same Toast employee record build_shifts_csv uses, so
    they match the names in a generated schedule.

    The window is the restaurant's own business day — BUSINESS_DAY_START_HOUR
    local to the same hour the next day, converted to UTC — not the UTC
    calendar date: a Pacific dinner clock-in after 5pm fell outside "today",
    and yesterday's evening fell inside it (DH2-8). An entry Toast files
    under another businessDate is not today's."""
    from time_utils import restaurant_tz, BUSINESS_DAY_START_HOUR
    tz = _tz_for(restaurant_id) or restaurant_tz(None)
    start_local = datetime.combine(business_date, datetime.min.time()).replace(
        hour=BUSINESS_DAY_START_HOUR, tzinfo=tz)
    end_local = start_local + timedelta(days=1) - timedelta(seconds=1)
    entries = fetch_time_entries(restaurant_id, business_date, business_date,
                                 start_at=start_local, end_at=end_local) or []
    want = business_date.strftime("%Y%m%d")
    entries = [e for e in entries if not str(e.get("businessDate") or "").strip()
               or str(e.get("businessDate")).strip() == want]
    _require_resolved(restaurant_id, entries)
    rows = []
    for entry in entries:
        name = _toast_name(entry.get("employee") or {})
        if not name:
            continue
        rows.append({"employee": name,
                     "role": (entry.get("jobReference") or {}).get("name", "Staff"),
                     "clocked_in_at": entry.get("inDate"),
                     # The punch's end once they clocked out (live labor
                     # to date reads it — labor_live), None while on.
                     "clocked_out_at": entry.get("outDate") or None,
                     "hourly_wage": entry.get("hourlyWage"),
                     # The GUID the person is keyed on (people.resolve_rows),
                     # so a clock-in matches the scheduled person whatever
                     # spelling either side carries (memory audit 9/29/26).
                     "external_id": (str((entry.get("employee") or {}).get("guid") or "").strip()
                                     or str((entry.get("employeeReference") or {}).get("guid") or "").strip()
                                     or None)})
    return rows


# ── the ticket archive (schedule audit 10/10/26 MARKET-9) ───────────────────
#
# pos_archive keeps one business date's tickets, sale lines, punches and
# payments per restaurant; labor standards, the Crew stage's busy/slow read,
# the measured sales curve (schedule_economics.measured_sales_curve reads
# tickets first) and every ticket-timing learner read it. Only RPOWER fed it.
# Toast's Orders API (ordersBulk) carries each order's open/close times,
# server, table, guest count, checks, selections and payments; the Labor
# API's time entries carry the punches. Read defensively: the shapes below
# are Toast's documented ones, not yet seen from a live Toast account here.
#
# What Toast does NOT give through these endpoints: kitchen fire and bump
# times (the KDS is a separate product), so fired_at/bumped_at stay None —
# never a guess. A comp is rung as a discount (fetch_day_sales), so it is a
# "discount" line carrying the discount's own name as its reason.

def _local_iso(ts, tz):
    """A Toast UTC stamp as the restaurant's local wall clock
    ("YYYY-MM-DDTHH:MM:SS", the archive's format), or None."""
    dt = _parse_toast_time(ts) if ts else None
    if dt is None:
        return None
    if dt.tzinfo is not None and tz is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _toast_base():
    return TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE


def _orders_bulk(restaurant_id, params: dict) -> list:
    """Every order ordersBulk returns for `params` (a businessDate, or a
    startDate/endDate modified window), paged to the end; ToastTruncated
    past PAGE_LIMIT pages — a partial day is never stored as a whole one."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    out, page = [], 1
    for _ in range(PAGE_LIMIT):
        resp = _http(requests.get, f"{_toast_base()}/orders/v2/ordersBulk",
                     headers=_headers(token, r.toast_restaurant_guid),
                     params=dict(params, page=page, pageSize=100), timeout=30)
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    else:
        raise ToastTruncated(f"Toast returned more than {PAGE_LIMIT * 100:,} orders for {params}")
    return out


def _table_names(restaurant_id) -> dict:
    """{table guid: name} from the config API, {} when it can't be read
    (the ticket is still archived, its table unnamed)."""
    from models import get_restaurant
    try:
        r = get_restaurant(restaurant_id)
        token = get_toast_token(restaurant_id)
        resp = _http(requests.get, f"{_toast_base()}/config/v2/tables",
                     headers=_headers(token, r.toast_restaurant_guid), timeout=30)
        resp.raise_for_status()
        return {t["guid"]: (t.get("name") or "").strip() or None for t in (resp.json() or [])
                if isinstance(t, dict) and t.get("guid")}
    except Exception:
        return {}


def _break_minutes(entry) -> float:
    total = 0.0
    for b in entry.get("breaks") or []:
        a, z = _parse_toast_time((b or {}).get("inDate")), _parse_toast_time((b or {}).get("outDate"))
        if a and z and z > a:
            total += (z - a).total_seconds() / 60.0
    return round(total, 1)


def _disc_line(line_id, ticket_id, iso, amount, name, at, stamp, extra=None):
    out = {"line_id": line_id, "ticket_id": ticket_id, "business_date": iso, "kind": "discount", "qty": 1.0,
           "sales": round(-amount, 2), "price": round(-amount, 2), "reason": (name or "").strip() or None,
           "item_at": at, "source_stamp": stamp}
    out.update(extra or {})
    return out


def archive_rows(restaurant_id: int, business_date) -> dict:
    """One Toast business date in pos_archive's provider-neutral shape:
    {"tickets", "lines", "punches", "payments", "payouts", "max_stamp"}.

    Tickets are orders (voided/deleted ones kept, `cancelled` 1); a ticket's
    net is its live checks' `amount` (after discounts, before tax), the same
    net fetch_day_sales reports. Lines: each selection (a voided one as a
    "void" line with its pre-discount price as the loss), each discount
    applied to a selection or a check as a "discount" line (negative), so a
    ticket's sale and discount lines sum to its net. Punches are the Labor
    API's time entries opened in the business day (BUSINESS_DAY_START_HOUR
    local to the same hour next day); pay = regular hours at the wage plus
    overtime hours at 1.5x it (Toast reports hours and the wage, not pay)."""
    if _is_demo(restaurant_id):
        raise NotImplementedError("Toast demo mode has no real day to archive")
    from time_utils import BUSINESS_DAY_START_HOUR
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(str(business_date)[:10])
    iso, ymd = day.isoformat(), day.strftime("%Y%m%d")
    tz = _tz_for(restaurant_id)
    orders = _orders_bulk(restaurant_id, {"businessDate": ymd})
    people = _fetch_people(restaurant_id, {}) if orders else {"employees": {}, "jobs": {}}
    emps = people.get("employees") or {}
    tables = _table_names(restaurant_id) if any((o.get("table") or {}).get("guid") for o in orders) else {}
    tickets, lines, payments, stamps = [], [], [], []
    for o in orders:
        oid = str(o.get("guid") or "").strip()
        bd = str(o.get("businessDate") or "").strip()
        if not oid or (bd and bd != ymd):
            continue
        dead_order = bool(o.get("voided") or o.get("deleted"))
        stamp = str(o.get("modifiedDate") or "")[:19] or None
        stamps.append(str(o.get("modifiedDate") or ""))
        opened = _local_iso(o.get("openedDate"), tz)
        net = disc_total = tax = tip = 0.0
        for ci, check in enumerate(o.get("checks") or []):
            dead = dead_order or bool(check.get("voided") or check.get("deleted"))
            if not dead:
                net += _num(check.get("amount"))
                tax += _num(check.get("taxAmount"))
                for di, d in enumerate(check.get("appliedDiscounts") or []):
                    amt = _num((d or {}).get("discountAmount"))
                    if amt:
                        disc_total += amt
                        lines.append(_disc_line(str((d or {}).get("guid") or f"{oid}:c{ci}:d{di}"), oid, iso, amt,
                                                (d or {}).get("name"), opened, stamp))
            for si, sel in enumerate(check.get("selections") or []):
                sid = str(sel.get("guid") or f"{oid}:c{ci}:s{si}")
                cat = sel.get("salesCategory") if isinstance(sel.get("salesCategory"), dict) else {}
                base_line = {"ticket_id": oid, "business_date": iso, "item_id": (sel.get("item") or {}).get("guid"),
                             "item_name": (sel.get("displayName") or "").strip() or None, "item_kind": None,
                             "qty": _num(sel.get("quantity")),
                             "profit_center": (cat.get("name") or "").strip() or None,
                             "item_at": _local_iso(sel.get("createdDate"), tz) or opened, "source_stamp": stamp}
                price = _num(sel.get("price"))
                if dead or sel.get("voided"):
                    lost = _num(sel.get("preDiscountPrice")) or price
                    lines.append(dict(base_line, line_id=sid, kind="void", sales=0.0, price=round(lost, 2),
                                      loss_amount=round(lost, 2), voided=1.0))
                    continue
                sel_disc = sum(_num((d or {}).get("discountAmount")) for d in (sel.get("appliedDiscounts") or []))
                lines.append(dict(base_line, line_id=sid, kind="sale", sales=round(price + sel_disc, 2),
                                  price=round(_num(sel.get("preDiscountPrice")) or (price + sel_disc), 2)))
                for di, d in enumerate(sel.get("appliedDiscounts") or []):
                    amt = _num((d or {}).get("discountAmount"))
                    if amt:
                        disc_total += amt
                        lines.append(_disc_line(str((d or {}).get("guid") or f"{sid}:d{di}"), oid, iso, amt,
                                                (d or {}).get("name"), base_line["item_at"], stamp,
                                                {"item_id": base_line["item_id"],
                                                 "item_name": base_line["item_name"]}))
            for pi, p in enumerate(check.get("payments") or []):
                kind = str((p or {}).get("type") or "").upper()
                ptip = _num((p or {}).get("tipAmount"))
                tip += 0.0 if dead else ptip
                payments.append({"payment_id": str((p or {}).get("guid") or f"{oid}:c{ci}:p{pi}"), "ticket_id": oid,
                                 "business_date": iso, "method": kind.title() or None,
                                 "is_cash": 1 if kind == "CASH" else 0,
                                 "is_card": 1 if kind in ("CREDIT", "DEBIT") else 0,
                                 "amount": round(_num((p or {}).get("amount")), 2), "tip": round(ptip, 2),
                                 "tip_fee": None})
        server = str((o.get("server") or {}).get("guid") or "").strip() or None
        table = str((o.get("table") or {}).get("guid") or "").strip() or None
        rc = o.get("revenueCenter") if isinstance(o.get("revenueCenter"), dict) else {}
        tickets.append({
            "ticket_id": oid, "business_date": iso, "ticket_no": str(o.get("displayNumber") or "") or None,
            "opened_at": opened, "closed_at": _local_iso(o.get("closedDate"), tz),
            "fired_at": None, "bumped_at": None, "need_at": None,
            "server_id": server, "server_name": (_toast_name(emps.get(server) or {}) or None) if server else None,
            "table_id": table, "table_name": tables.get(table) if table else None, "room_name": None, "is_bar": 0,
            "guest_count": int(_num(o.get("numberOfGuests"))), "entree_count": None, "bev_count": None,
            "net_sales": round(net, 2), "discount": round(disc_total, 2), "tip": round(tip, 2), "grat": None,
            "tax": round(tax, 2), "mealtime": None, "profit_center": (rc.get("name") or None),
            "cancelled": 1 if dead_order else 0, "source_stamp": stamp})
    # Punches: the business day's time entries, the person and job resolved.
    start_local = datetime.combine(day, datetime.min.time()).replace(hour=BUSINESS_DAY_START_HOUR, tzinfo=tz)
    end_local = start_local + timedelta(days=1) - timedelta(seconds=1)
    entries = fetch_time_entries(restaurant_id, day, day, start_at=start_local, end_at=end_local) or []
    entries = [e for e in entries if not str(e.get("businessDate") or "").strip()
               or str(e.get("businessDate")).strip() == ymd]
    if entries:
        _require_resolved(restaurant_id, entries)
    punches = []
    for e in entries:
        pid = str(e.get("guid") or "").strip()
        if not pid:
            continue
        emp = e.get("employee") or {}
        eid = (str(emp.get("guid") or "").strip() or str((e.get("employeeReference") or {}).get("guid") or "").strip()
               or None)
        wage = _num(e.get("hourlyWage"))
        reg, ot = _num(e.get("regularHours")), _num(e.get("overtimeHours"))
        stamps.append(str(e.get("modifiedDate") or ""))
        punches.append({
            "punch_id": pid, "business_date": iso, "employee_id": eid, "employee_name": _toast_name(emp) or None,
            "job_id": str((e.get("jobReference") or {}).get("guid") or "") or None,
            "role": (e.get("jobReference") or {}).get("name") or None,
            "clock_in": _local_iso(e.get("inDate"), tz), "clock_out": _local_iso(e.get("outDate"), tz),
            "reg_hours": reg, "ot_hours": ot, "dt_hours": 0.0, "reg_rate": wage or None,
            "ot_rate": round(wage * 1.5, 2) if wage else None,
            "pay": round(reg * wage + ot * wage * 1.5, 2) if wage else None,
            "ot_pay": round(ot * wage * 1.5, 2) if wage else None,
            "tips": round(_num(e.get("declaredCashTips")) + _num(e.get("nonCashTips")), 2), "tips_net": None,
            "grats": round(_num(e.get("cashGratuityServiceCharges")) + _num(e.get("nonCashGratuityServiceCharges")), 2),
            "break_minutes": _break_minutes(e), "meal_minutes": None, "rest_minutes": None,
            "edited_by": None, "edited_at": None, "edit_what": None, "is_station": 0,
            "source_stamp": str(e.get("modifiedDate") or "")[:19] or None})
    return {"tickets": tickets, "lines": lines, "punches": punches, "payments": payments, "payouts": [],
            "max_stamp": (max(s for s in stamps if s)[:19] if any(stamps) else None)}


def changed_business_dates(restaurant_id: int, since_utc: datetime, until_utc: datetime) -> set:
    """Business dates with an order Toast modified between two UTC moments
    (ordersBulk's startDate/endDate window reads the order's modified date):
    a check re-opened and closed after the night was archived shows up here
    and the day is archived again (pos_archive restatements)."""
    fmt = "%Y-%m-%dT%H:%M:%S.000+0000"
    orders = _orders_bulk(restaurant_id, {"startDate": since_utc.astimezone(timezone.utc).strftime(fmt),
                                          "endDate": until_utc.astimezone(timezone.utc).strftime(fmt)})
    out = set()
    for o in orders:
        bd = str(o.get("businessDate") or "").strip()
        if len(bd) == 8 and bd.isdigit():
            out.add(f"{bd[:4]}-{bd[4:6]}-{bd[6:]}")
    return out


# ── the schedule written back to Toast (schedule audit 10/10/26 MARKET-7) ───
#
# Toast enforces a schedule at its time clock (early clock-in, late clock-out)
# for shifts an integration partner writes through the Labor API's shifts
# resource. Cavnar AI's Toast credentials are a standard API client with read
# scopes: writing shifts needs Toast to admit Cavnar AI to its integration-
# partner programme and grant the labor write scope. Until then the push is
# refused with SCHEDULE_WRITE_EXTERNAL (TOAST_SCHEDULE_WRITE stays off) — the
# preview, which only READS (employees, jobs), is real and shows exactly what
# would be sent and what Toast would refuse.
SCHEDULE_WRITE_ENV = "TOAST_SCHEDULE_WRITE"
SCHEDULE_WRITE_EXTERNAL = ("Toast hasn't granted Cavnar AI the labor write access its integration partners get, so "
                           "the schedule can't be written to Toast's time clock yet. Nothing was sent.")


def schedule_write_enabled() -> bool:
    return os.getenv(SCHEDULE_WRITE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _schedule_body(restaurant_id, shifts, people=None):
    """([Toast Shift bodies], [{employee, why}] skipped) — each Cavnar shift
    as Toast's documented Shift entity: the employee and job by GUID
    (resolved by exact name from Toast's own lists; a name two current
    employees share matches neither), inDate/outDate in UTC from the
    restaurant's local wall clock. `_employee`/`_start`/`_end` ride along
    for the caller and are stripped before anything is sent."""
    import staff_settings as _ss
    people = people or _fetch_people(restaurant_id, {})
    tz = _tz_for(restaurant_id)
    by_name, dup = {}, set()
    for guid, e in (people.get("employees") or {}).items():
        if e.get("deleted"):
            continue
        k = _ss.name_key(_toast_name(e))
        if not k:
            continue
        if k in by_name:
            dup.add(k)
        by_name[k] = guid
    jobs = {(t or "").strip().lower(): g for g, t in (people.get("jobs") or {}).items() if t}
    body, skipped = [], []
    for s in shifts or []:
        who = (s.get("employee") or "").strip()
        k = _ss.name_key(who)
        guid = str(s.get("toast_employee_guid") or "").strip() or (by_name.get(k) if k not in dup else None)
        job_name = (s.get("job") or s.get("role") or "").strip()
        job = jobs.get(job_name.lower())
        start, end = s.get("start"), s.get("end")
        if not (guid and job and start and end):
            skipped.append({"employee": who or "(unnamed)",
                            "why": ("two Toast employees share that name" if k in dup else
                                    "no Toast employee with that name" if not guid else
                                    f"Toast has no job \"{job_name}\"" if not job else "missing times")})
            continue
        try:
            a = datetime.fromisoformat(str(start)[:19]).replace(tzinfo=tz).astimezone(timezone.utc)
            z = datetime.fromisoformat(str(end)[:19]).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            skipped.append({"employee": who, "why": "times not readable"})
            continue
        body.append({"entityType": "Shift",
                     "employeeReference": {"guid": guid, "entityType": "RestaurantUser"},
                     "jobReference": {"guid": job, "entityType": "RestaurantJob"},
                     "inDate": a.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "outDate": z.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "_employee": who, "_start": str(start)[:19], "_end": str(end)[:19]})
    return body, skipped


def _sendable(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def preview_labor_schedule(restaurant_id: int, shifts: list, job_codes: dict = None) -> dict:
    """What a push WOULD write to Toast, checked against Toast's own employee
    and job lists — reads only, never sends. RPOWER's preview shape: {ok,
    sent: False, would_send, employees, shifts, skipped, problems} plus
    `write_enabled` and, while Toast has not granted the write, `external`."""
    body, skipped = _schedule_body(restaurant_id, shifts)
    problems, spans = [], {}
    for b in body:
        shift = {"inTime": b["_start"], "outTime": b["_end"]}
        if b["outDate"] <= b["inDate"]:
            problems.append({"employee": b["_employee"], "shift": shift, "problem": "ends before it starts"})
        spans.setdefault(b["employeeReference"]["guid"], []).append((b["inDate"], b["outDate"], b))
    for rows in spans.values():
        rows.sort(key=lambda x: x[0])
        for (_a1, z1, b1), (a2, _z2, b2) in zip(rows, rows[1:]):
            if a2 < z1:
                problems.append({"employee": b2["_employee"], "shift": {"inTime": b2["_start"], "outTime": b2["_end"]},
                                 "problem": f"overlaps {b1['_start']}–{b1['_end']}"})
    out = {"ok": bool(body) and not problems, "sent": False, "would_send": [_sendable(b) for b in body],
           "employees": len(spans), "shifts": len(body), "skipped": skipped, "problems": problems,
           "write_enabled": schedule_write_enabled()}
    if not schedule_write_enabled():
        out["external"] = SCHEDULE_WRITE_EXTERNAL
    return out


def push_labor_schedule(restaurant_id: int, shifts: list, job_codes: dict = None) -> dict:
    """Write the week's shifts to Toast (POST /labor/v1/shifts, one shift per
    call) — refused with SCHEDULE_WRITE_EXTERNAL until Toast grants the write
    scope and TOAST_SCHEDULE_WRITE is set. UNVERIFIED against a live Toast
    account: confirm the entity shape and the re-push rule (Toast keeps its
    own shift GUIDs; a corrected week must update or delete, not add twice)
    on the first partner sandbox before turning it on. Returns {ok,
    employees, shifts, skipped, failed, error}."""
    if not schedule_write_enabled():
        return {"ok": False, "external": True, "error": SCHEDULE_WRITE_EXTERNAL, "skipped": []}
    import config as _config_gate
    if not _config_gate.outbound_sends_allowed():
        # A live store's schedule, never from staging (blind re-audit 10/27/26).
        return {"ok": False, "skipped": [], "error": _config_gate.refused_send("toast_schedule")}
    from models import get_restaurant
    body, skipped = _schedule_body(restaurant_id, shifts)
    if not body:
        return {"ok": False, "error": "No shifts had a Toast employee and job to push.", "skipped": skipped}
    r = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    failed = []
    for b in body:
        try:
            resp = _http(requests.post, f"{_toast_base()}/labor/v1/shifts",
                         headers=_headers(token, r.toast_restaurant_guid), json=_sendable(b), timeout=30, tries=1)
        except Exception as e:
            failed.append({"employee": b["_employee"], "start": b["_start"], "why": f"couldn't reach Toast: {e}"})
            continue
        if resp.status_code in (401, 403):
            return {"ok": False, "skipped": skipped, "error": SCHEDULE_WRITE_EXTERNAL, "external": True}
        if not resp.ok:
            failed.append({"employee": b["_employee"], "start": b["_start"],
                           "why": f"Toast returned HTTP {resp.status_code}: {resp.text[:160]}"})
    return {"ok": not failed, "employees": len({b["employeeReference"]["guid"] for b in body}),
            "shifts": len(body) - len(failed), "skipped": skipped, "failed": failed,
            "error": None if not failed else f"{len(failed)} shift(s) Toast did not take"}
