"""
square.py — Square POS API client for Cavnar AI

Pulls time entries (timecards) and sales data via Square's Labor and Orders APIs.
Normalizes output into the same shifts CSV format that labor.py consumes.

Credentials stored per restaurant:
  square_access_token  — personal access token from Square developer dashboard
  square_location_id   — location ID from Square (restaurants can have multiple)
"""
import csv, io, requests
from datetime import datetime, timezone, timedelta, date
from typing import Optional

SQUARE_BASE = "https://connect.squareup.com/v2"


# ── Auth / connection helpers ──────────────────────────────────────────────────

def _headers(restaurant_id: int) -> dict:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not r.square_access_token:
        raise ValueError("Square not connected for this restaurant")
    return {
        "Authorization": f"Bearer {r.square_access_token}",
        "Content-Type":  "application/json",
        "Square-Version": "2024-01-17",
    }


def is_connected(restaurant_id: int) -> bool:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return bool(r and r.square_access_token and r.square_location_id)


def test_credentials(access_token: str, location_id: str) -> dict:
    """Validate credentials by fetching the location."""
    try:
        resp = requests.get(
            f"{SQUARE_BASE}/locations/{location_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Square-Version": "2024-01-17",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            name = resp.json().get("location", {}).get("name", "Unknown")
            return {"ok": True, "location_name": name}
        return {"ok": False, "error": f"Square returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_team_members(restaurant_id: int) -> dict:
    """Return {team_member_id: {name, job_title}} dict."""
    headers = _headers(restaurant_id)
    members = {}
    cursor = None
    while True:
        body = {"limit": 200}
        if cursor:
            body["cursor"] = cursor
        resp = requests.post(f"{SQUARE_BASE}/team-members/search", headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for tm in data.get("team_members", []):
            members[tm["id"]] = {
                "name": f"{tm.get('given_name','')} {tm.get('family_name','')}".strip() or "Unknown",
                # assigned_locations.assignment_type is where the member may
                # work ("ALL_CURRENT_AND_FUTURE_LOCATIONS"), not their job; the
                # shift's own wage title is the role (MOD-LAB-6).
                "job_title": "Staff",
            }
        cursor = data.get("cursor")
        if not cursor:
            break
    return members


def _fetch_shifts(restaurant_id: int, start_dt: datetime, end_dt: datetime) -> list:
    """Fetch time card shifts from Square Labor API."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    headers = _headers(restaurant_id)
    shifts = []
    cursor = None
    while True:
        body = {
            "filter": {
                "location_ids": [r.square_location_id],
                "start": {"start_at": start_dt.isoformat(), "end_at": end_dt.isoformat()},
                "status": "CLOSED",
            },
            "limit": 200,
        }
        if cursor:
            body["cursor"] = cursor
        resp = requests.post(f"{SQUARE_BASE}/labor/shifts/search", headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        shifts.extend(data.get("shifts", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return shifts


def _tz_for(restaurant_id):
    from time_utils import restaurant_tz
    try:
        from models import get_restaurant
        return restaurant_tz(get_restaurant(restaurant_id))
    except Exception:
        return restaurant_tz(None)


def _local_date(stamp, tz, restaurant=None):
    """The restaurant's BUSINESS date for a Square UTC timestamp: its local
    time, filed under the service it belongs to (time_utils.business_date —
    a 12:30am check is still last night's). pos.py's sales contract."""
    from time_utils import business_date
    try:
        local = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(tz)
    except Exception:
        return None
    return business_date(restaurant, local.replace(tzinfo=None)).isoformat()


def _money_cents(order, field):
    try:
        return int((order.get(field) or {}).get("amount") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _order_net_cents(order) -> int:
    """NET sales for one Square order, in cents: total_money less tax, tips
    and service charges (pos.py's sales contract, CA3 F11). total_money is
    what the guest paid — tax and tip included — so labor % against it read
    low and was not comparable with Toast or RPOWER. Discounts are already
    out of total_money."""
    return (_money_cents(order, "total_money") - _money_cents(order, "total_tax_money")
            - _money_cents(order, "total_tip_money") - _money_cents(order, "total_service_charge_money"))


def _fetch_daily_sales(restaurant_id: int, start_date: date, end_date: date) -> dict:
    """Return {business_date: net_sales_dollars} from Square Orders, each
    order dated by the restaurant's own business date (Square's created_at
    is UTC, so an 8pm order used to land on tomorrow's sales), net of tax,
    tips and service charges, to the cent."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    tz = _tz_for(restaurant_id)
    headers = _headers(restaurant_id)
    sales = {}
    cursor = None
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
    end_dt   = datetime.combine(end_date, datetime.max.time().replace(microsecond=0)).replace(tzinfo=tz)
    while True:
        body = {
            "location_ids": [r.square_location_id],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "created_at": {
                            "start_at": start_dt.isoformat(),
                            "end_at":   end_dt.isoformat(),
                        }
                    },
                    "state_filter": {"states": ["COMPLETED"]},
                }
            },
            "limit": 500,
        }
        if cursor:
            body["cursor"] = cursor
        resp = requests.post(f"{SQUARE_BASE}/orders/search", headers=headers, json=body, timeout=15)
        if resp.status_code != 200:
            # A later page failing (a 429) used to `break` and the partial
            # sales were saved as whole days (MOD-LAB-7). Fail the sync; the
            # previous data stays.
            raise RuntimeError(f"Square orders search returned {resp.status_code} partway through; nothing was saved")
        data = resp.json()
        for order in data.get("orders", []):
            created = _local_date(order.get("created_at", ""), tz, r)
            if not created:
                continue
            sales[created] = sales.get(created, 0) + _order_net_cents(order)
        cursor = data.get("cursor")
        if not cursor:
            break
    # Cents to dollars, to the cent (it was rounded to whole dollars).
    return {k: round(v / 100, 2) for k, v in sales.items()}


# ── CSV builder ────────────────────────────────────────────────────────────────

def build_shifts_csv(restaurant_id: int, days: int = 60) -> Optional[str]:
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_d  = start_dt.date()
    end_d    = end_dt.date()

    tz      = _tz_for(restaurant_id)
    from models import get_restaurant as _gr
    from time_utils import business_date
    restaurant = _gr(restaurant_id)
    team    = _fetch_team_members(restaurant_id)
    shifts  = _fetch_shifts(restaurant_id, start_dt, end_dt)
    sales   = _fetch_daily_sales(restaurant_id, start_d, end_d)

    if not shifts:
        return None

    rows = []
    DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    for s in shifts:
        start_str = s.get("start_at", "")
        end_str   = s.get("end_at", "")
        if not start_str or not end_str:
            continue
        try:
            start_p = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_p   = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            continue

        # Square's times are UTC; the business day is the restaurant's —
        # the service the shift starts in (time_utils.business_date).
        start_p, end_p = start_p.astimezone(tz), end_p.astimezone(tz)
        bdate      = business_date(restaurant, start_p.replace(tzinfo=None))
        date_str   = bdate.isoformat()
        day_name   = DAY_NAMES[bdate.weekday()]
        hours      = round((end_p - start_p).total_seconds() / 3600, 2)
        tid        = s.get("team_member_id", "")
        member     = team.get(tid, {"name": "Unknown", "job_title": "Staff"})
        # Blank, never 0, for a day with no sales figure (pos.py's contract).
        daily_sale = sales.get(date_str, "")

        rows.append({
            "date":             date_str,
            "day":              day_name,
            "employee":         member["name"],
            "role":             ((s.get("wage") or {}).get("title") or member["job_title"]),
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
            return {"ok": False, "error": "No shift data returned from Square"}
        row_count = csv_str.count("\n") - 1
        import pos
        pos.save_synced_shifts(restaurant_id, csv_str, "square")
        update_restaurant(restaurant_id, {
            "square_last_synced": datetime.now(timezone.utc).isoformat(),
            "square_sync_error":  None,
            "pos_system":         "Square",
        })
        return {"ok": True, "rows": max(0, row_count)}
    except Exception as e:
        try:
            update_restaurant(restaurant_id, {"square_sync_error": str(e)[:500]})
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
