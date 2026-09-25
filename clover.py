"""
clover.py — Clover POS API client for Cavnar AI

Pulls employee timesheets and order totals via Clover's REST API.
Normalizes output into the same shifts CSV format that labor.py consumes.

Credentials stored per restaurant:
  clover_merchant_id  — merchant ID from Clover dashboard
  clover_api_token    — API token from Clover developer dashboard
"""
import csv, io, requests
from datetime import datetime, timezone, timedelta
from typing import Optional

CLOVER_BASE = "https://api.clover.com/v3"


# ── Auth / connection helpers ──────────────────────────────────────────────────

def _headers(restaurant_id: int) -> dict:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not r.clover_api_token:
        raise ValueError("Clover not connected for this restaurant")
    return {
        "Authorization": f"Bearer {r.clover_api_token}",
        "Content-Type":  "application/json",
    }


def _mid(restaurant_id: int) -> str:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return r.clover_merchant_id or ""


def is_connected(restaurant_id: int) -> bool:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return bool(r and r.clover_api_token and r.clover_merchant_id)


def test_credentials(merchant_id: str, api_token: str) -> dict:
    """Validate by fetching the merchant info."""
    try:
        resp = requests.get(
            f"{CLOVER_BASE}/merchants/{merchant_id}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            name = resp.json().get("name", "Unknown")
            return {"ok": True, "merchant_name": name}
        return {"ok": False, "error": f"Clover returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _http(fn, url, **kwargs):
    """One provider call through pos.http_call: retried on a timeout, a
    dropped connection, 429 and 5xx (Retry-After honoured), never on 401 or
    403 (DH2-5). Every caller still names its timeout."""
    import pos
    return pos.http_call(fn, url, **kwargs)


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_employees(restaurant_id: int) -> dict:
    """Return {employee_id: {name, role}} dict."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    employees = {}
    offset = 0
    while True:
        resp = _http(requests.get, 
            f"{CLOVER_BASE}/merchants/{mid}/employees",
            headers=headers,
            params={"limit": 200, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("elements", [])
        for e in items:
            employees[e["id"]] = {
                "name": e.get("name") or f"{e.get('nickname','')}".strip() or "Unknown",
                "role": e.get("role", "Staff").title(),
            }
        if len(items) < 200:
            break
        offset += 200
    return employees


def _fetch_shifts(restaurant_id: int, start_ms: int, end_ms: int) -> list:
    """Fetch timesheets from Clover."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    shifts = []
    offset = 0
    while True:
        resp = _http(requests.get, 
            f"{CLOVER_BASE}/merchants/{mid}/shifts",
            headers=headers,
            params={
                "filter": f"inTime>={start_ms}&outTime<={end_ms}",
                "limit":  200,
                "offset": offset,
                "expand": "employee",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            # A failed page used to `break` and the shifts so far were saved
            # as the whole window. Fail the sync instead; the previous data
            # stays (Square's MOD-LAB-7, CA3 F11).
            raise RuntimeError(f"Clover shifts returned {resp.status_code} partway through; nothing was saved")
        data = resp.json()
        items = data.get("elements", [])
        shifts.extend(items)
        if len(items) < 200:
            break
        offset += 200
    return shifts


def _tz_and_restaurant(restaurant_id):
    from time_utils import restaurant_tz
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
    except Exception:
        r = None
    return restaurant_tz(r), r


def _business_date_ms(ms, tz, restaurant):
    """The restaurant's business date for a Clover epoch-millisecond stamp —
    local time, filed under its service (time_utils.business_date). It was
    the UTC calendar date: a Chicago Monday 7:30pm shift became Tuesday."""
    from time_utils import business_date
    local = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
    return business_date(restaurant, local.replace(tzinfo=None))


def _order_net_cents(order) -> int:
    """NET sales for one Clover order, in cents (pos.py's sales contract):
    the order total less the tax recorded on its payments. Clover's order
    `total` includes tax; tips live on the payment, outside the total. Only
    the payments' taxAmount is subtracted — verify against the merchant's
    own net figure on the first live Clover restaurant (there is none
    today)."""
    try:
        total = int(order.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    tax = 0
    pays = order.get("payments") or {}
    for p in (pays.get("elements") if isinstance(pays, dict) else pays) or []:
        try:
            tax += int((p or {}).get("taxAmount") or 0)
        except (TypeError, ValueError):
            continue
    return total - tax


def _fetch_daily_sales(restaurant_id: int, start_ms: int, end_ms: int) -> dict:
    """Return {business_date: net_sales_dollars} from Clover orders: net of
    tax, dated by the restaurant's business date, to the cent. A page that
    fails fails the sync — never a partial window saved as a whole one."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    tz, restaurant = _tz_and_restaurant(restaurant_id)
    sales = {}
    offset = 0
    while True:
        resp = _http(requests.get, 
            f"{CLOVER_BASE}/merchants/{mid}/orders",
            headers=headers,
            params={
                "filter": f"createdTime>={start_ms}&createdTime<={end_ms}&paymentState=PAID",
                "expand": "payments",
                "limit":  500,
                "offset": offset,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Clover orders returned {resp.status_code} partway through; nothing was saved")
        data = resp.json()
        items = data.get("elements", [])
        for order in items:
            ms = order.get("createdTime")
            if not ms:
                continue
            date_str = _business_date_ms(ms, tz, restaurant).isoformat()
            sales[date_str] = sales.get(date_str, 0) + _order_net_cents(order)
        if len(items) < 500:
            break
        offset += 500
    # Clover amounts are in cents; kept to the cent (was whole dollars).
    return {k: round(v / 100, 2) for k, v in sales.items()}


# ── CSV builder ────────────────────────────────────────────────────────────────

def build_shifts_csv(restaurant_id: int, days: int = 60) -> Optional[str]:
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    employees = _fetch_employees(restaurant_id)
    shifts    = _fetch_shifts(restaurant_id, start_ms, end_ms)
    sales     = _fetch_daily_sales(restaurant_id, start_ms, end_ms)
    tz, restaurant = _tz_and_restaurant(restaurant_id)

    if not shifts:
        return None

    rows = []
    DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    for s in shifts:
        in_ms  = s.get("inTime")
        out_ms = s.get("outTime")
        if not in_ms or not out_ms:
            continue

        # Local times, filed under the business date the shift starts in —
        # they were UTC, so an evening shift landed on the next day.
        start_p = datetime.fromtimestamp(in_ms / 1000, tz=timezone.utc).astimezone(tz)
        end_p   = datetime.fromtimestamp(out_ms / 1000, tz=timezone.utc).astimezone(tz)
        hours   = round((end_p - start_p).total_seconds() / 3600, 2)
        if hours <= 0:
            continue

        bdate      = _business_date_ms(in_ms, tz, restaurant)
        date_str   = bdate.isoformat()
        day_name   = DAY_NAMES[bdate.weekday()]
        emp_obj    = s.get("employee") or {}
        emp_id     = emp_obj.get("id", "")
        emp        = employees.get(emp_id, {"name": emp_obj.get("name","Unknown"), "role": "Staff"})
        # Blank, never 0, for a day with no sales figure (pos.py's contract).
        daily_sale = sales.get(date_str, "")

        rows.append({
            "date":             date_str,
            "day":              day_name,
            "employee":         emp["name"],
            "role":             emp["role"],
            "shift_start":      start_p.strftime("%H:%M"),
            "shift_end":        end_p.strftime("%H:%M"),
            "scheduled_hours":  hours,
            "actual_hours":     hours,
            "sales":            daily_sale,
            "notes":            "",
        })

    if not rows:
        return None

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["date","day","employee","role","shift_start","shift_end","scheduled_hours","actual_hours","sales","notes"])
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def sync_to_db(restaurant_id: int) -> dict:
    from models import save_client_data, update_restaurant
    try:
        csv_str = build_shifts_csv(restaurant_id, days=60)
        if not csv_str:
            # An empty pull is a failed sync, written like RPOWER's so the
            # freshness registry and pos_health see it (re-audit B3#15).
            update_restaurant(restaurant_id, {
                "clover_sync_error": "No shift data returned for the last 60 days"})
            return {"ok": False, "error": "No shift data returned from Clover"}
        row_count = csv_str.count("\n") - 1
        import pos
        pos.save_synced_shifts(restaurant_id, csv_str, "clover")
        update_restaurant(restaurant_id, {
            "clover_last_synced": datetime.now(timezone.utc).isoformat(),
            "clover_sync_error":  None,
            "pos_system":         "Clover",
        })
        return {"ok": True, "rows": max(0, row_count)}
    except Exception as e:
        try:
            update_restaurant(restaurant_id, {"clover_sync_error": str(e)[:500]})
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
