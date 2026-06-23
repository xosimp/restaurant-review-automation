"""
clover.py — Clover POS API client for Cavnar AI

Pulls employee timesheets and order totals via Clover's REST API.
Normalizes output into the same shifts CSV format that labor.py consumes.

Credentials stored per restaurant:
  clover_merchant_id  — merchant ID from Clover dashboard
  clover_api_token    — API token from Clover developer dashboard
"""
import os, csv, io, requests
from datetime import datetime, timezone, timedelta, date
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


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_employees(restaurant_id: int) -> dict:
    """Return {employee_id: {name, role}} dict."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    employees = {}
    offset = 0
    while True:
        resp = requests.get(
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
        resp = requests.get(
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
            break
        data = resp.json()
        items = data.get("elements", [])
        shifts.extend(items)
        if len(items) < 200:
            break
        offset += 200
    return shifts


def _fetch_daily_sales(restaurant_id: int, start_ms: int, end_ms: int) -> dict:
    """Return {date_str: total_sales} from Clover orders."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    sales = {}
    offset = 0
    while True:
        resp = requests.get(
            f"{CLOVER_BASE}/merchants/{mid}/orders",
            headers=headers,
            params={
                "filter": f"createdTime>={start_ms}&createdTime<={end_ms}&paymentState=PAID",
                "limit":  500,
                "offset": offset,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data.get("elements", [])
        for order in items:
            ts = order.get("createdTime", 0) / 1000
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            total = order.get("total", 0)
            sales[date_str] = sales.get(date_str, 0) + total
        if len(items) < 500:
            break
        offset += 500
    # Clover totals are in cents
    return {k: round(v / 100) for k, v in sales.items()}


# ── CSV builder ────────────────────────────────────────────────────────────────

def build_shifts_csv(restaurant_id: int, days: int = 60) -> Optional[str]:
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    employees = _fetch_employees(restaurant_id)
    shifts    = _fetch_shifts(restaurant_id, start_ms, end_ms)
    sales     = _fetch_daily_sales(restaurant_id, start_ms, end_ms)

    if not shifts:
        return None

    rows = []
    DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    for s in shifts:
        in_ms  = s.get("inTime")
        out_ms = s.get("outTime")
        if not in_ms or not out_ms:
            continue

        start_p = datetime.fromtimestamp(in_ms / 1000, tz=timezone.utc)
        end_p   = datetime.fromtimestamp(out_ms / 1000, tz=timezone.utc)
        hours   = round((end_p - start_p).total_seconds() / 3600, 2)
        if hours <= 0:
            continue

        date_str   = start_p.date().isoformat()
        day_name   = DAY_NAMES[start_p.weekday()]
        emp_obj    = s.get("employee") or {}
        emp_id     = emp_obj.get("id", "")
        emp        = employees.get(emp_id, {"name": emp_obj.get("name","Unknown"), "role": "Staff"})
        daily_sale = sales.get(date_str, 0)

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
            return {"ok": False, "error": "No shift data returned from Clover"}
        row_count = csv_str.count("\n") - 1
        save_client_data(restaurant_id, "shifts", csv_str, source="clover")
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
