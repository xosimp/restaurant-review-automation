"""
toast.py — Toast POS API client for Cavnar AI

Handles authentication (client_credentials), labor shift fetching,
and daily sales aggregation. All data is normalized into the same
dict-list format that labor.py already understands, so nothing in
the analysis engine changes.

Toast API base: https://ws-api.toasttab.com (production)
                https://ws-sandbox.toasttab.com (sandbox)
"""
import os, csv, io, json, requests
from datetime import datetime, timezone, timedelta, date
from typing import Optional

TOAST_BASE    = os.getenv("TOAST_API_BASE", "https://ws-api.toasttab.com")
TOAST_SANDBOX = "https://ws-sandbox.toasttab.com"

_DEMO_ID = "demo"  # any credential field equal to this triggers demo mode


# ── Demo mode ─────────────────────────────────────────────────────────────────

def _is_demo(restaurant_id: int) -> bool:
    """True when any credential field is set to the literal string 'demo'."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return bool(r and (
            r.toast_client_id == _DEMO_ID or
            r.toast_client_secret == _DEMO_ID or
            r.toast_restaurant_guid == _DEMO_ID
        ))
    except Exception:
        return False


def _demo_shifts_csv() -> str:
    """
    Generate a realistic 8-week CSV of shift + sales data for a busy
    Italian casual-dining restaurant (Gia Mia style). Used in demo mode
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


# ── Data fetching ──────────────────────────────────────────────────────────────

def _headers(token: str, restaurant_guid: str) -> dict:
    return {
        "Authorization":               f"Bearer {token}",
        "Toast-Restaurant-External-ID": restaurant_guid,
        "Content-Type":                "application/json",
    }


def fetch_time_entries(restaurant_id: int, start_date: date, end_date: date) -> list:
    """
    Pull clock-in/clock-out time entries from Toast Labor API with pagination.
    Toast returns up to 100 entries per page; we loop until no nextPageToken.
    Returns a flat list of all raw Toast timeEntry dicts.
    """
    from models import get_restaurant

    r     = get_restaurant(restaurant_id)
    token = get_toast_token(restaurant_id)
    base  = TOAST_SANDBOX if os.getenv("TOAST_SANDBOX", "").lower() in ("1", "true") else TOAST_BASE

    # Toast expects RFC 3339 with +00:00 offset
    start_iso = datetime.combine(start_date, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    end_iso   = datetime.combine(end_date, datetime.max.time().replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

    all_entries = []
    page_token  = None
    page_limit  = 20  # safety cap — 20 pages × 100 = 2,000 entries, well above 60 days

    for _ in range(page_limit):
        params = {"startDate": start_iso, "endDate": end_iso, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(
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

    resp = requests.get(
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
        net = day.get("netSales", 0) or 0
        sales_by_date[iso] = float(net)
    return sales_by_date


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


def normalise_entries(time_entries: list, sales_by_date: dict) -> list:
    """
    Convert raw Toast timeEntry objects into dicts matching the CSV schema
    that labor.py understands:
      date, day, employee, role, shift_start, shift_end,
      scheduled_hours, actual_hours, sales, notes
    """
    rows = []
    for entry in time_entries:
        try:
            # Employee name
            emp_obj = entry.get("employee", {})
            first   = emp_obj.get("firstName", "")
            last    = (emp_obj.get("lastName") or "")[:1] + "."  # e.g. "T."
            employee = f"{first} {last}".strip() if first else "Unknown"

            # Role / job
            job_ref = entry.get("jobReference", {}) or {}
            role    = job_ref.get("name", "Staff")

            # Times
            in_dt  = _parse_toast_time(entry.get("inDate"))
            out_dt = _parse_toast_time(entry.get("outDate"))
            if not in_dt:
                continue  # clock-out only or corrupt entry

            # Business date (use in_dt date as the day)
            entry_date = in_dt.date()
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
                actual_hours = 0.0

            # Scheduled hours — use declared schedule if present, else actual
            sched_in  = _parse_toast_time(entry.get("scheduledInDate"))
            sched_out = _parse_toast_time(entry.get("scheduledOutDate"))
            if sched_in and sched_out:
                delta_s = (sched_out - sched_in).total_seconds() / 3600
                scheduled_hours = round(max(0, delta_s), 2)
            else:
                scheduled_hours = actual_hours

            shift_start = in_dt.strftime("%H:%M") if in_dt else ""
            shift_end   = out_dt.strftime("%H:%M") if out_dt else ""

            sales = sales_by_date.get(date_str, 0)

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

    time_entries = fetch_time_entries(restaurant_id, start, end)
    sales        = fetch_business_days(restaurant_id, start, end)
    rows         = normalise_entries(time_entries, sales)

    if not rows:
        return ""

    fieldnames = ["date","day","employee","role","shift_start","shift_end",
                  "scheduled_hours","actual_hours","sales","notes"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
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
            return {"ok": False, "error": "No shift data returned from Toast"}

        row_count = csv_str.count("\n") - 1  # subtract header
        save_client_data(restaurant_id, "shifts", csv_str, source="toast")
        update_restaurant(restaurant_id, {
            "toast_last_synced": datetime.now(timezone.utc).isoformat(),
            "toast_sync_error":  None,
        })
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
