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
            # The employee's Clover id (memory audit 9/29/26, identity);
            # Clover's shifts carry no schedule.
            "employee_ext_id":  emp_id,
            "employee_named":   "1" if emp_id in employees else "0",
            "schedule_known":   "0",
        })

    if not rows:
        return None

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["date","day","employee","role","shift_start","shift_end","scheduled_hours","actual_hours","sales","notes",
                                        "employee_ext_id","employee_named","schedule_known"])
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


# ── the ticket archive (schedule audit 10/10/26 MARKET-9) ───────────────────
#
# pos_archive's provider-neutral rows from Clover's orders (with line items,
# discounts, payments and the employee who rang them) and shifts. Clover's
# REST API carries no table, guest count or kitchen times, so those stay
# None, and no pay rate on a shift, so a punch's pay is None (never a
# guessed wage). Amounts are cents.

def _dollars(v) -> float:
    try:
        return round(int(v or 0) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _elements(v) -> list:
    return (v.get("elements") if isinstance(v, dict) else v) or []


def _local_ms(ms, tz):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _orders_window(restaurant_id, field, start_ms, end_ms) -> list:
    """Every order whose `field` (createdTime / modifiedTime) falls in the
    window, expanded, paged to the end; a failed page fails the read."""
    mid = _mid(restaurant_id)
    headers = _headers(restaurant_id)
    out, offset = [], 0
    while True:
        resp = _http(requests.get, f"{CLOVER_BASE}/merchants/{mid}/orders", headers=headers,
                     params={"filter": f"{field}>={start_ms}&{field}<{end_ms}",
                             "expand": "lineItems,lineItems.discounts,discounts,payments,payments.tender,employee",
                             "limit": 500, "offset": offset}, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Clover orders returned {resp.status_code} partway through; nothing was saved")
        items = resp.json().get("elements", [])
        out.extend(items)
        if len(items) < 500:
            break
        offset += 500
    return out


def _discount_cents(d, base_cents) -> int:
    """A Clover discount in cents: `amount` (negative cents) or a
    `percentage` of the line/order it is on."""
    try:
        if d.get("amount") is not None:
            return abs(int(d.get("amount") or 0))
        if d.get("percentage") is not None:
            return int(round(abs(float(d["percentage"])) / 100.0 * int(base_cents or 0)))
    except (TypeError, ValueError):
        pass
    return 0


def archive_rows(restaurant_id: int, business_date) -> dict:
    """One business date as pos_archive rows: {"tickets", "lines",
    "punches", "payments", "payouts", "max_stamp"}. Orders created in the
    business day (BUSINESS_DAY_START_HOUR local to the same hour next day);
    a ticket's net is pos.py's contract (_order_net_cents: total less the
    tax on its payments). Lines: each line item (a refunded one as a
    "refund" line, its price the loss) and each discount as a negative
    "discount" line. Punches: shifts that clocked in during the day."""
    from time_utils import BUSINESS_DAY_START_HOUR
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(str(business_date)[:10])
    iso = day.isoformat()
    tz, _r = _tz_and_restaurant(restaurant_id)
    start = datetime.combine(day, datetime.min.time()).replace(hour=BUSINESS_DAY_START_HOUR, tzinfo=tz)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=1)).timestamp() * 1000)
    employees = _fetch_employees(restaurant_id)
    tickets, lines, payments, stamps = [], [], [], []
    for o in _orders_window(restaurant_id, "createdTime", start_ms, end_ms):
        oid = str(o.get("id") or "").strip()
        if not oid:
            continue
        stamp = _local_ms(o.get("modifiedTime"), tz)
        stamps.append(stamp or "")
        opened = _local_ms(o.get("createdTime"), tz)
        disc = 0
        for li in _elements(o.get("lineItems")):
            price = int(li.get("price") or 0)
            qty = (float(li.get("unitQty")) / 1000.0) if li.get("unitQty") else 1.0
            gross = int(round(price * qty))
            refunded = bool(li.get("refunded"))
            lines.append({"line_id": str(li.get("id") or f"{oid}:li"), "ticket_id": oid, "business_date": iso,
                          "item_id": (li.get("item") or {}).get("id"),
                          "item_name": (li.get("name") or "").strip() or None, "item_kind": None,
                          "kind": "refund" if refunded else "sale", "qty": qty,
                          "sales": 0.0 if refunded else _dollars(gross), "price": _dollars(price),
                          "loss_amount": _dollars(gross) if refunded else None, "item_at": opened,
                          "source_stamp": stamp})
            for d in _elements(li.get("discounts")):
                c = _discount_cents(d, gross)
                if c and not refunded:
                    disc += c
                    lines.append({"line_id": str(d.get("id") or f"{li.get('id')}:d"), "ticket_id": oid,
                                  "business_date": iso, "kind": "discount", "qty": 1.0, "sales": -_dollars(c),
                                  "price": -_dollars(c), "reason": (d.get("name") or "").strip() or None,
                                  "item_at": opened, "source_stamp": stamp})
        for d in _elements(o.get("discounts")):
            c = _discount_cents(d, o.get("total"))
            if c:
                disc += c
                lines.append({"line_id": str(d.get("id") or f"{oid}:d"), "ticket_id": oid, "business_date": iso,
                              "kind": "discount", "qty": 1.0, "sales": -_dollars(c), "price": -_dollars(c),
                              "reason": (d.get("name") or "").strip() or None, "item_at": opened,
                              "source_stamp": stamp})
        tip = tax = 0
        paid_at = max((int(p.get("createdTime") or 0) for p in _elements(o.get("payments"))), default=0) or None
        for p in _elements(o.get("payments")):
            label = ((p.get("tender") or {}).get("label") or "").strip()
            tip += int(p.get("tipAmount") or 0)
            tax += int(p.get("taxAmount") or 0)
            payments.append({"payment_id": str(p.get("id") or f"{oid}:p"), "ticket_id": oid, "business_date": iso,
                             "method": label or None, "is_cash": 1 if label.lower() == "cash" else 0,
                             "is_card": 1 if "card" in label.lower() else 0, "amount": _dollars(p.get("amount")),
                             "tip": _dollars(p.get("tipAmount")), "tip_fee": None})
        emp = str((o.get("employee") or {}).get("id") or "").strip() or None
        state = str(o.get("state") or "").lower()
        tickets.append({
            "ticket_id": oid, "business_date": iso, "ticket_no": None, "opened_at": opened,
            # Clover keeps no close time: a paid (locked) order closed with
            # its last payment.
            "closed_at": _local_ms(paid_at, tz) if (state == "locked" and paid_at) else None,
            "fired_at": None, "bumped_at": None, "need_at": None, "server_id": emp,
            "server_name": (employees.get(emp) or {}).get("name") if emp else None, "table_id": None,
            "table_name": None, "room_name": None, "is_bar": 0, "guest_count": None, "entree_count": None,
            "bev_count": None, "net_sales": _dollars(_order_net_cents(o)), "discount": _dollars(disc),
            "tip": _dollars(tip), "grat": None, "tax": _dollars(tax), "mealtime": None, "profit_center": None,
            "cancelled": 0, "source_stamp": stamp})
    punches = []
    for s in _fetch_shifts(restaurant_id, start_ms, end_ms + 16 * 3600 * 1000):
        sid = str(s.get("id") or "").strip()
        if not sid or not s.get("inTime") or not (start_ms <= int(s["inTime"]) < end_ms):
            continue
        eid = str((s.get("employee") or {}).get("id") or "").strip() or None
        a, z = _local_ms(s.get("inTime"), tz), _local_ms(s.get("outTime"), tz) if s.get("outTime") else None
        hours = (round((int(s["outTime"]) - int(s["inTime"])) / 3600000.0, 2) if s.get("outTime") else None)
        punches.append({
            "punch_id": sid, "business_date": iso, "employee_id": eid,
            "employee_name": (employees.get(eid) or {}).get("name") if eid else None, "job_id": None,
            "role": (employees.get(eid) or {}).get("role") if eid else None, "clock_in": a, "clock_out": z,
            "reg_hours": hours, "ot_hours": None, "dt_hours": None, "reg_rate": None, "ot_rate": None, "pay": None,
            "ot_pay": None, "tips": _dollars(s.get("cashTipsCollected")) or None, "tips_net": None, "grats": None,
            "break_minutes": None, "meal_minutes": None, "rest_minutes": None, "edited_by": None,
            "edited_at": None, "edit_what": None, "is_station": 0, "source_stamp": _local_ms(s.get("modifiedTime"), tz)})
    return {"tickets": tickets, "lines": lines, "punches": punches, "payments": payments, "payouts": [],
            "max_stamp": (max(x for x in stamps if x)[:19] if any(stamps) else None)}


def changed_business_dates(restaurant_id: int, since_utc: datetime, until_utc: datetime) -> set:
    """Business dates of orders Clover modified between two UTC moments
    (the orders filter on modifiedTime)."""
    tz, restaurant = _tz_and_restaurant(restaurant_id)
    out = set()
    for o in _orders_window(restaurant_id, "modifiedTime", int(since_utc.timestamp() * 1000),
                            int(until_utc.timestamp() * 1000)):
        if o.get("createdTime"):
            out.add(_business_date_ms(int(o["createdTime"]), tz, restaurant).isoformat())
    return out
