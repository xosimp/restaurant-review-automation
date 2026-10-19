"""
demo_store.api — RPOWER's Core API, answered for Halsted & Rye.

rpower._request hands a demo-store token's GET here instead of to
rpowerpos.com: same paths, same parameters, same record shapes and paging
(1,000 a page), so the RPOWER code above it runs unchanged. Today's records
appear as the Chicago clock reaches them — a check opens when its party
sits down and closes when it pays, a punch has no clock-out until the shift
ends, the close-day record arrives after closing — and a day not yet begun
has nothing. Read-only: there is no write path here at all.
"""
from datetime import date, datetime, timedelta

from demo_store import calendar as cal
from demo_store import day as D
from demo_store import world as w

PAGE_SIZE = 1000
NEVER = D.NEVER

# Tests pin the clock: a naive Chicago wall-clock datetime, or None for now.
FIXED_NOW = None


def now_local() -> datetime:
    if FIXED_NOW is not None:
        return FIXED_NOW
    return datetime.now(D.TZ).replace(tzinfo=None)


def business_today(now=None) -> date:
    return ((now or now_local()) - timedelta(hours=D.BUSINESS_DAY_START_HOUR)).date()


def _d(s) -> date:
    return date.fromisoformat(str(s)[:10])


def _clean(rows):
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ── one day, cut at the clock ──────────────────────────────────────────────

_ST_NAME = {mid: name for name, mid in w.ST.items()}


def _cut(day: date, now: datetime) -> dict:
    """The day's records as RPOWER holds them at `now`."""
    if day > business_today(now):
        return {"tickets": [], "lines": [], "items": [], "payments": [], "payouts": [], "punches": [],
                "closed": False}
    full = D.business_day(day)
    if day < business_today(now) and (full["closed_at"] is None or full["closed_at"] <= now):
        return dict(full, closed=True)
    stamp = D._stamp(now)
    lines = [l for l in full["lines"] if l["_at"] <= now]
    # An open ticket's totals are what has been rung on it so far, as RPOWER holds them - not the night's
    # final check (owner, 10/19/26: the archive read at midnight saw full-night totals over half-rung lines,
    # and the nightly reconcile paged a gap that was the simulation's).
    rung = {}
    for l in lines:
        st = _ST_NAME.get(str(l.get("slstype_mid")))
        if st:
            k = rung.setdefault(l["ticket_rid"], {})
            k[st] = round(k.get(st, 0.0) + float(l.get("sales") or 0), 2)
    tickets = []
    for t in full["tickets"]:
        if t["_open"] > now:
            continue
        if t["_close"] > now:
            r = rung.get(t["rid"], {})
            sale, disc, comp = r.get("Sale", 0.0), r.get("Discount", 0.0), r.get("Comp", 0.0)
            t = dict(t, close_dttm=NEVER, tip=0, items=round(sale, 2), discount=round(disc, 2),
                     type_sale=round(sale, 2), type_discnt=round(disc, 2), type_comp=round(comp, 2),
                     type_error=round(r.get("Error Correct", 0.0), 2),
                     tax=round(max(0.0, sale + disc) * w.TAX_RATE, 2))
        tickets.append(t)
    items = [i for i in full["items"] if i["_at"] <= now]
    payments = [p for p in full["payments"] if p["_at"] <= now]
    payouts = [p for p in full["payouts"] if p["_at"] <= now]
    punches = []
    for p in full["punches"]:
        if p["in_dttm"] > stamp:
            continue
        if p["out_dttm"] > stamp:
            p = dict(p, out_dttm=NEVER, reg_hours=0, ot_hours=0, reg_pay=0, ot_pay=0, tips_total=0, tip_net=0,
                     edit_dttm=NEVER, edit_what="")
        punches.append(p)
    return {"tickets": tickets, "lines": lines, "items": items, "payments": payments, "payouts": payouts,
            "punches": punches, "closed": bool(full["closed_at"] and full["closed_at"] <= now)}


# ── the lists ──────────────────────────────────────────────────────────────

def _base(m, n=0):
    return {"mid": m, "hasha": m, "cg": w.CG, "time_stamp": "2026-01-05T15:00:00", "short_mid": int(m[-9:]),
            "rec_flags": 0, "xtags": "", "base_xtags": "", "seq_id": n}


def _store():
    return [{"mid": w.STORE_MID, "hasha": w.STORE_MID, "cg": w.CG, "serial_number": w.CG, "name": w.STORE_NAME,
             "store_id": "HR1", "tag_name": "HALSTED RYE", "ot_dow": 1, "week_dow": 1, "open_days": "YYYYYYY",
             "timezone": w.TIMEZONE, "time_stamp": "2026-01-05T15:00:00"}]


def _sales_types():
    out = []
    for i, (name, flags) in enumerate(w.SALES_TYPES):
        r = _base(w.ST[name], i + 1)
        r.update(name=name, abbr5=name[:5], abbr1=name[:1])
        for f in ("type_sale", "type_nonsale", "type_return", "type_refund", "type_comp", "type_waste", "type_error",
                  "type_promo", "type_discnt", "type_hash", "type_hashdisc", "type_grat", "type_hidgrat",
                  "type_housegrat", "type_fee", "type_tax", "impacts_sales", "impacts_costs"):
            r[f] = flags.get(f, 0)
        out.append(r)
    return out


def _departments():
    return [dict(_base(w.DEP[d], i + 1), name=d, rpt_data="") for i, d in enumerate(w.DEPARTMENTS)]


def _categories():
    out = []
    for i, (c, (dep, is_bar)) in enumerate(w.CATEGORIES.items()):
        out.append(dict(_base(w.CAT[c], i + 1), name=c, slsdep_mid=w.DEP[dep], alt_qty_unit="Each",
                        alt_rpt_unit="Each", aqu_in_aru=1, discitm_mid="1", report_id="", xtype="0", is_bar=is_bar,
                        is_hash=0, rpt_data=""))
    return out


def _menu_items():
    out = []
    for i, m in enumerate(w.menu()):
        st = "Discount" if m["kind"] == "discount" else ("Non Sale" if m["price"] == 0 and m["kind"] != "discount"
                                                          else "Sale")
        out.append(dict(_base(m["mid"], i + 1), name=m["name"], mittype_mid="1", slscat_mid=w.CAT[m["category"]],
                        slstype_mid=w.ST[st], discitm_mid="1", itemizer_bit=0, itemizer_bits=0, alt_name1="",
                        alt_name2="", alt_name3="", aqu_count=1, last_cost=0, upc="", plu=str(1000 + i),
                        is_mod=0, is_scale=0))
    return out


def _prices():
    out = []
    lv = {1: w.mid("pricelvl", 1), 2: w.mid("pricelvl", 2)}
    for i, m in enumerate(w.menu()):
        if m["price"] > 0:
            out.append(dict(_base(w.mid("price", i * 2 + 1)), store_mid=w.STORE_MID, menuitem_mid=m["mid"],
                            prclvl_mid=lv[1], price=m["price"]))
        if m.get("hh_price"):
            out.append(dict(_base(w.mid("price", i * 2 + 2)), store_mid=w.STORE_MID, menuitem_mid=m["mid"],
                            prclvl_mid=lv[2], price=m["hh_price"]))
    return out


def _jobs():
    out = []
    for i, (name, (ext, rate, tipped, salaried, can_server)) in enumerate(w.JOBS.items()):
        out.append(dict(_base(w.JOB[name], i + 1), name=name, ext_id=ext, pyrlcat_mid=w.mid("pyrlcat", 1),
                        lcenter_mid="3", reg_rate=rate, is_salary=1 if salaried else 0,
                        can_server=1 if can_server else 0, is_server=1 if name == "Server" else 0, is_driver=0,
                        is_busser=1 if name == "Busser" else 0, is_operator=0,
                        is_cashier=1 if name == "Bartender" else 0, is_host=1 if name == "Host" else 0,
                        day_start_time="0.05:00:00"))
    return out


def _employees():
    out = []
    for i, e in enumerate(w.employees()):
        out.append(dict(_base(e["mid"], i + 1), name=f"{e['last']}, {e['first']}", phone="", payroll_id=e["payroll_id"],
                        store_mid_any="1", lname=e["last"], fname=e["first"], mname="", aname="", term_date=NEVER,
                        mobile=""))
    return out


def _employee_jobs():
    out, n = [], 0
    for e in w.employees():
        for j, primary in [(e["job"], 1)] + [(a, 0) for a in e["also"]]:
            n += 1
            rate = e["rate"] if primary else (w.JOBS[j][1] or e["rate"])
            out.append(dict(_base(w.mid("empjob", n)), store_mid=w.STORE_MID, emp_mid=e["mid"], job_mid=w.JOB[j],
                            is_primary=primary, reg_rate=rate, is_salary=1 if w.JOBS[j][3] else 0, is_inactive=0,
                            can_server=1 if w.JOBS[j][4] else 0, is_server=1 if j == "Server" else 0, is_driver=0,
                            is_busser=0, is_operator=0, is_cashier=0, is_host=0))
    return out


def _simple(kind, names, **extra):
    return [dict(_base(w.mid(kind, i + 1), i + 1), name=n, **extra) for i, n in enumerate(names)]


def _rooms():
    return [dict(_base(w.mid("room", i + 1), i + 1), store_mid=w.STORE_MID, name=r, is_bar=v[0])
            for i, (r, v) in enumerate(w.ROOMS.items())]


def _tables():
    out = []
    for i, (r, v) in enumerate(w.ROOMS.items()):
        for j, t in enumerate(v[1]):
            out.append(dict(_base(w.mid("table", i * 100 + j + 1), j + 1), store_mid=w.STORE_MID, name=t,
                            room_mid=w.mid("room", i + 1)))
    return out


def _payment_methods():
    return [dict(_base(w.mid("paymeth", i + 1), i + 1), name=n, pmtdep_mid="1", is_cash=c, is_cc=cc, is_deposit=0,
                 tender_key=0, report_id="", xtype=x) for i, (n, c, cc, x) in enumerate(w.PAYMENT_METHODS)]


def _reasons():
    out = [dict(_base("1", 0), name="", slstype_mid="1", is_system=1, xtype="0")]
    for i, (name, kind, _made) in enumerate(w.REASONS):
        out.append(dict(_base(w.REASON[name], i + 2), name=name,
                        slstype_mid=w.ST["Comp" if kind == "comp" else "Error Correct"], is_system=0, xtype="0"))
    return out


_LISTS = {
    "store/get": _store,
    "salestype/getbycg": _sales_types,
    "salesdepartment/getbycg": _departments,
    "salescategory/getbycg": _categories,
    "menuitem/getbycg": _menu_items,
    "menuitemprice/getbystore": _prices,
    "job/getbycg": _jobs,
    "employee/getbycg": _employees,
    "employeejob/getbystore": _employee_jobs,
    "voidreason/getbycg": _reasons,
    "payrollcategory/getbycg": lambda: _simple("pyrlcat", ["Hourly"], pyrldep_mid="1"),
    "mealtime/getbycg": lambda: _simple("meal", [n for n, _s in w.MEALTIMES], xtype="0"),
    "profitcenter/getbycg": lambda: _simple("pcenter", w.PROFIT_CENTERS, xtype="0"),
    "pricelevel/getbycg": lambda: _simple("pricelvl", w.PRICE_LEVELS),
    "payoutcategory/getbycg": lambda: _simple("pocat", w.PAYOUT_CATEGORIES, slstype_mid="1", xtype="0", is_payout=1,
                                              is_system=0, is_cash=1),
    "paymentmethod/getbycg": _payment_methods,
    "room/getbystore": _rooms,
    "table/getbystore": _tables,
}

_DAY_KIND = {"ticketsales/getbybusinessdate": "lines", "ticket/getbybusinessdate": "tickets",
             "ticketpayment/getbybusinessdate": "payments", "ticketitem/getbybusinessdate": "items"}


def _all(path: str, params: dict, now: datetime) -> list:
    p = {str(k).lower(): v for k, v in (params or {}).items()}
    if path in _LISTS:
        return _LISTS[path]()
    if path in _DAY_KIND:
        out = []
        for d in _days(_d(p["startdate"]), _d(p["enddate"])):
            out.extend(_cut(d, now)[_DAY_KIND[path]])
        return out
    if path == "closeday/getbybusinessdate":
        out = []
        for d in _days(_d(p["startdate"]), _d(p["enddate"])):
            c = _cut(d, now)
            full = D.business_day(d) if c["closed"] else None
            if c["closed"] and full["closed_at"]:
                iso = f"{d.isoformat()}T00:00:00"
                out.append({"rid": "73%s0000000000" % d.strftime("%Y%m%d"), "cg": w.CG, "store_mid": w.STORE_MID,
                            "from_date": iso, "from_shift": "A", "thru_date": iso, "thru_shift": "A",
                            "dttm": D._stamp(full["closed_at"]), "time_stamp": D._utc(full["closed_at"])})
        return out
    if path == "timeclock/getbydaterange":
        out = []
        for d in _days(_d(p["startdate"]), _d(p["enddate"]) - timedelta(days=1)):
            out.extend(_cut(d, now)["punches"])
        return out
    if path == "payout/getbydaterange":
        out = []
        for d in _days(_d(p["startdate"]), _d(p["enddate"])):
            out.extend(_cut(d, now)["payouts"])
        return out
    if path == "ticketsales/getbytimestamp":
        lo, hi = _d(p["startdate"]), _d(p["enddate"])
        out = []
        for d in _days(lo - timedelta(days=1), hi):
            for l in _cut(d, now)["lines"]:
                if lo.isoformat() <= l["time_stamp"][:10] <= hi.isoformat():
                    out.append(l)
        return out
    if path == "ticket/getbyneeddatetime":
        lo, hi = _d(p["startdate"]), _d(p["enddate"])
        out = []
        for d in _days(lo - timedelta(days=4), hi):
            for t in _cut(d, now)["tickets"]:
                if t["need_dttm"] != NEVER and lo.isoformat() <= t["need_dttm"][:10] < hi.isoformat():
                    out.append(t)
        return out
    return []


def respond(token: str, path: str, params: dict = None):
    """RPOWER's answer to one GET, one page of it."""
    path = str(path or "").strip("/").lower()
    rows = _clean(_all(path, params or {}, now_local()))
    if path == "store/get":
        return rows
    try:
        page = int((params or {}).get("pagenumber") or 1)
    except (TypeError, ValueError):
        page = 1
    return rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
