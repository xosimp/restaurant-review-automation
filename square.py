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


def _http(fn, url, **kwargs):
    """One provider call through pos.http_call: retried on a timeout, a
    dropped connection, 429 and 5xx (Retry-After honoured), never on 401 or
    403 (DH2-5). Every caller still names its timeout."""
    import pos
    return pos.http_call(fn, url, **kwargs)


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
        resp = _http(requests.post, f"{SQUARE_BASE}/team-members/search", headers=headers, json=body, timeout=15)
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
        resp = _http(requests.post, f"{SQUARE_BASE}/labor/shifts/search", headers=headers, json=body, timeout=15)
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
        resp = _http(requests.post, f"{SQUARE_BASE}/orders/search", headers=headers, json=body, timeout=15)
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
            # The team member's Square id (memory audit 9/29/26, identity);
            # Square's timecards carry no schedule.
            "employee_ext_id":  tid,
            "employee_named":   "1" if tid in team else "0",
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
                "square_sync_error": "No shift data returned for the last 60 days"})
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


# ── the ticket archive (schedule audit 10/10/26 MARKET-9) ───────────────────
#
# pos_archive's provider-neutral rows from Square's Orders API (each order's
# opened/closed times, line items, discounts, tenders) and Labor API (shifts
# with wage, breaks and declared tips). Square's Orders API carries no server,
# table or guest count, so those stay None (labor standards that need covers
# say they are not measured, never 0), and no kitchen fire/bump times.

def _cents(obj, field=None) -> float:
    m = (obj or {}).get(field) if field else obj
    try:
        return round(int((m or {}).get("amount") or 0) / 100.0, 2)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _local_iso(stamp, tz):
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _day_window(restaurant_id, day):
    """(tz, restaurant, aware start, aware end) of one business day:
    BUSINESS_DAY_START_HOUR local to the same hour the next day."""
    from models import get_restaurant
    from time_utils import BUSINESS_DAY_START_HOUR
    tz = _tz_for(restaurant_id)
    start = datetime.combine(day, datetime.min.time()).replace(hour=BUSINESS_DAY_START_HOUR, tzinfo=tz)
    return tz, get_restaurant(restaurant_id), start, start + timedelta(days=1)


def _search_orders(restaurant_id, date_filter: dict, sort_field: str) -> list:
    """Every order /orders/search returns for one date filter, paged to the
    end; a page that fails fails the read (never a partial day)."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    headers = _headers(restaurant_id)
    out, cursor = [], None
    while True:
        body = {"location_ids": [r.square_location_id], "limit": 500,
                "query": {"filter": {"date_time_filter": date_filter},
                          "sort": {"sort_field": sort_field, "sort_order": "ASC"}}}
        if cursor:
            body["cursor"] = cursor
        resp = _http(requests.post, f"{SQUARE_BASE}/orders/search", headers=headers, json=body, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Square orders search returned {resp.status_code} partway through; nothing was saved")
        data = resp.json()
        out.extend(data.get("orders", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def _search_shifts(restaurant_id, start_dt, end_dt) -> list:
    """Every Labor shift (open or closed) that started in [start, end)."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    headers = _headers(restaurant_id)
    out, cursor = [], None
    while True:
        body = {"query": {"filter": {"location_ids": [r.square_location_id],
                                     "start": {"start_at": start_dt.isoformat(), "end_at": end_dt.isoformat()}}},
                "limit": 200}
        if cursor:
            body["cursor"] = cursor
        resp = _http(requests.post, f"{SQUARE_BASE}/labor/shifts/search", headers=headers, json=body, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Square shifts search returned {resp.status_code} partway through; nothing was saved")
        data = resp.json()
        out.extend(data.get("shifts", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def archive_rows(restaurant_id: int, business_date) -> dict:
    """One business date as pos_archive rows: {"tickets", "lines",
    "punches", "payments", "payouts", "max_stamp"}. A ticket is an order
    opened in the business day; its net is pos.py's contract
    (_order_net_cents: total less tax, tips and service charges); a
    CANCELED order is kept with `cancelled` 1 and no net. Lines: each line
    item at its gross (base price x quantity) and each discount as a
    negative "discount" line. Punches: Labor shifts started in the day,
    pay = the shift's hourly wage x its unpaid-break-adjusted hours."""
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(str(business_date)[:10])
    iso = day.isoformat()
    tz, restaurant, start, end = _day_window(restaurant_id, day)
    orders = _search_orders(restaurant_id, {"created_at": {"start_at": start.isoformat(), "end_at": end.isoformat()}},
                            "CREATED_AT")
    tickets, lines, payments, stamps = [], [], [], []
    for o in orders:
        oid = str(o.get("id") or "").strip()
        if not oid:
            continue
        stamp = str(o.get("updated_at") or "")[:19] or None
        stamps.append(str(o.get("updated_at") or ""))
        cancelled = str(o.get("state") or "").upper() == "CANCELED"
        opened = _local_iso(o.get("created_at"), tz)
        for li in o.get("line_items") or []:
            lid = str(li.get("uid") or "")
            try:
                qty = float(li.get("quantity") or 0)          # Square sends quantity as a string
            except (TypeError, ValueError):
                qty = 0.0
            gross = _cents(li, "gross_sales_money")
            lines.append({"line_id": f"{oid}:{lid}", "ticket_id": oid, "business_date": iso,
                          "item_id": li.get("catalog_object_id"), "item_name": (li.get("name") or "").strip() or None,
                          "item_kind": None, "kind": "void" if cancelled else "sale", "qty": qty,
                          "sales": 0.0 if cancelled else gross, "price": _cents(li, "base_price_money"),
                          "loss_amount": gross if cancelled else None, "item_at": opened, "source_stamp": stamp})
        for d in o.get("discounts") or []:
            amt = _cents(d, "applied_money")
            if amt and not cancelled:
                lines.append({"line_id": f"{oid}:{d.get('uid') or d.get('name') or 'discount'}", "ticket_id": oid,
                              "business_date": iso, "kind": "discount", "qty": 1.0, "sales": round(-amt, 2),
                              "price": round(-amt, 2), "reason": (d.get("name") or "").strip() or None,
                              "item_at": opened, "source_stamp": stamp})
        for t in o.get("tenders") or []:
            kind = str(t.get("type") or "").upper()
            payments.append({"payment_id": str(t.get("id") or f"{oid}:tender"), "ticket_id": oid,
                             "business_date": iso, "method": kind.title() or None,
                             "is_cash": 1 if kind == "CASH" else 0, "is_card": 1 if kind == "CARD" else 0,
                             "amount": _cents(t, "amount_money"), "tip": _cents(t, "tip_money"), "tip_fee": None})
        tickets.append({
            "ticket_id": oid, "business_date": iso, "ticket_no": None, "opened_at": opened,
            "closed_at": _local_iso(o.get("closed_at"), tz) if o.get("closed_at") else None,
            "fired_at": None, "bumped_at": None, "need_at": None, "server_id": None, "server_name": None,
            "table_id": None, "table_name": None, "room_name": None, "is_bar": 0, "guest_count": None,
            "entree_count": None, "bev_count": None,
            "net_sales": 0.0 if cancelled else round(_order_net_cents(o) / 100.0, 2),
            "discount": _cents(o, "total_discount_money"), "tip": _cents(o, "total_tip_money"), "grat": None,
            "tax": _cents(o, "total_tax_money"), "mealtime": None, "profit_center": None,
            "cancelled": 1 if cancelled else 0, "source_stamp": stamp})
    team = _fetch_team_members(restaurant_id)
    punches = []
    for s in _search_shifts(restaurant_id, start, end):
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        tid = s.get("team_member_id") or s.get("employee_id")
        wage = s.get("wage") or {}
        rate = _cents(wage, "hourly_rate") or None
        a, z = _local_iso(s.get("start_at"), tz), _local_iso(s.get("end_at"), tz) if s.get("end_at") else None
        unpaid = 0.0
        brk = 0.0
        for b in s.get("breaks") or []:
            ba, bz = _local_iso(b.get("start_at"), tz), _local_iso(b.get("end_at"), tz) if b.get("end_at") else None
            if ba and bz:
                m = (datetime.fromisoformat(bz) - datetime.fromisoformat(ba)).total_seconds() / 60.0
                brk += m
                unpaid += 0.0 if b.get("is_paid") else m
        hours = None
        if a and z:
            hours = round(max(0.0, (datetime.fromisoformat(z) - datetime.fromisoformat(a)).total_seconds() / 3600.0
                              - unpaid / 60.0), 2)
        stamps.append(str(s.get("updated_at") or ""))
        punches.append({
            "punch_id": sid, "business_date": iso, "employee_id": tid,
            "employee_name": (team.get(tid) or {}).get("name") if tid else None, "job_id": None,
            "role": (wage.get("title") or None), "clock_in": a, "clock_out": z, "reg_hours": hours,
            "ot_hours": None, "dt_hours": None, "reg_rate": rate, "ot_rate": None,
            "pay": round(hours * rate, 2) if (hours is not None and rate) else None, "ot_pay": None,
            "tips": _cents(s, "declared_cash_tip_money") or None, "tips_net": None, "grats": None,
            "break_minutes": round(brk, 1), "meal_minutes": None, "rest_minutes": None, "edited_by": None,
            "edited_at": None, "edit_what": None, "is_station": 0,
            "source_stamp": str(s.get("updated_at") or "")[:19] or None})
    return {"tickets": tickets, "lines": lines, "punches": punches, "payments": payments, "payouts": [],
            "max_stamp": (max(x for x in stamps if x)[:19] if any(stamps) else None)}


def changed_business_dates(restaurant_id: int, since_utc: datetime, until_utc: datetime) -> set:
    """Business dates of the orders Square updated between two UTC moments
    (the orders search's updated_at filter): a day re-posted after it was
    archived is archived again."""
    from models import get_restaurant
    from time_utils import business_date
    tz = _tz_for(restaurant_id)
    r = get_restaurant(restaurant_id)
    out = set()
    for o in _search_orders(restaurant_id, {"updated_at": {"start_at": since_utc.isoformat(),
                                                           "end_at": until_utc.isoformat()}}, "UPDATED_AT"):
        try:
            local = datetime.fromisoformat(str(o.get("created_at")).replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        out.add(business_date(r, local.replace(tzinfo=None)).isoformat())
    return out


# ── during service (schedule audit 10/10/26 MARKET-10, MARKET-24) ──────────
#
# The intraday pulse, the coverage check and live labor read a POS while the
# restaurant trades; only Toast and RPOWER answered. Square's Orders and Labor
# APIs answer during service too: today's orders so far, and today's shifts
# with their clock-in (an open shift has no end yet).

class SquareNoSalesYet(RuntimeError):
    """Square answered, with nothing posted yet today (not a POS that
    didn't answer — intraday.capture reads `not_yet`)."""
    not_yet = True
    provider = "square"


def fetch_sales_today(restaurant_id: int, business_date) -> float:
    """Net sales so far for the business date (pos.py's net contract:
    completed orders' total less tax, tips and service charges)."""
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(str(business_date)[:10])
    _tz, _r, start, end = _day_window(restaurant_id, day)
    orders = _search_orders(restaurant_id, {"created_at": {"start_at": start.isoformat(), "end_at": end.isoformat()}},
                            "CREATED_AT")
    done = [o for o in orders if str(o.get("state") or "").upper() == "COMPLETED"]
    if not done:
        raise SquareNoSalesYet(f"Square has no completed orders for {day.isoformat()} yet")
    return round(sum(_order_net_cents(o) for o in done) / 100.0, 2)


def fetch_clock_ins_today(restaurant_id: int, business_date) -> list:
    """Everyone who clocked in this business day: [{"employee", "role",
    "clocked_in_at", "clocked_out_at", "hourly_wage", "external_id"}] —
    the shape intraday.coverage_gaps and labor_live read."""
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(str(business_date)[:10])
    _tz, _r, start, end = _day_window(restaurant_id, day)
    shifts = _search_shifts(restaurant_id, start, end)
    team = _fetch_team_members(restaurant_id) if shifts else {}
    out = []
    for s in shifts:
        tid = s.get("team_member_id") or s.get("employee_id")
        name = (team.get(tid) or {}).get("name")
        if not name or name == "Unknown" or not s.get("start_at"):
            continue
        wage = s.get("wage") or {}
        out.append({"employee": name, "role": wage.get("title") or "Staff", "clocked_in_at": s.get("start_at"),
                    "clocked_out_at": s.get("end_at") or None, "hourly_wage": _cents(wage, "hourly_rate") or None,
                    "external_id": tid})
    return out
