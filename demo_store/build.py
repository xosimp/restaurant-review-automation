"""
demo_store.build — create Halsted & Rye and fill its history.

Run by scripts/demo_store_build.py (on Railway: one plain `railway ssh`).
Idempotent: the account is found by name and the demo flag, every step
replaces what it wrote, nothing touches any other restaurant. Every POS
figure goes through the real RPOWER code (the simulated store answers it);
the purchasing, recipes, reviews and the rest go through each module's own
import path, the way a real client's arrive.
"""
import logging
import os
from datetime import date, timedelta

import demo_store
from demo_store import api, world as w

log = logging.getLogger("demo_store.build")

OWNER_EMAIL = "maya@halstedandrye.example"      # .example: reserved, never deliverable
USERNAME = "halsted"
ARCHIVE_DAYS = 120                              # ticket-level archive
HISTORY_DAYS = 400                              # daily sales and labor


def _say(msg):
    print(f"[demo_store] {msg}", flush=True)


def find(db_path=None):
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        row = conn.execute("SELECT id FROM restaurants WHERE name=? AND COALESCE(is_demo,0)=1 ORDER BY id LIMIT 1",
                           (demo_store.NAME,)).fetchone()
    finally:
        conn.close()
    return int(row["id"]) if row else None


_STARTED = {"at": None}


def ensure_account() -> int:
    """The restaurant row, connected to the simulated store."""
    import models
    import rpower
    from datetime import datetime as _dt
    _STARTED["at"] = _STARTED["at"] or _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rid = find()
    if rid is None:
        rid = models.create_restaurant(models.Restaurant(
            name=demo_store.NAME, owner_email=OWNER_EMAIL, owner_name="Maya Castellanos", is_demo=1,
            module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1, service_tier="full",
            timezone=w.TIMEZONE, hourly_rate=17.0, labor_target_pct=28.0))
        _say(f"created {demo_store.NAME} as id={rid}")
    models.update_restaurant(rid, {
        "billing_status": "internal", "pos_system": "rpower", "neighborhood": "Lakeview, Chicago",
        "latitude": w.LAT, "longitude": w.LNG, "food_cost_target": 30.0, "bar_cost_target": 20.0,
        "beer_cost_target": 22.0, "liquor_cost_target": 18.0, "wine_cost_target": 28.0,
        "comp_void_target_pct": 1.5, "monthly_revenue_target": 360000.0, "labor_target_pct": 28.0,
        "vibe": "Neighborhood sports bar three blocks from Wrigley Field: scratch kitchen, 10 drafts, every game on.",
        "known_for": "Smash burgers, Italian beef, wings, Old Style on draft, Bears Sundays",
        "sign_off_name": "Maya & the Halsted & Rye team", "owner_name": "Maya Castellanos",
        "internal_notes": "SHOWCASE DEMO (demo_store). Fictional restaurant; nothing here is a client's data. "
                          "Never sends (demo_store.is_simulated).",
        "rpower_token": demo_store.TOKEN, "week_start_day": 0, "dsr_enabled": 1,
        "fiscal_week_start_dow": 0, "fiscal_period_scheme": "calendar",
    })
    res = rpower.bootstrap(rid)
    if not res.get("ok"):
        raise RuntimeError(f"bootstrap failed: {res}")
    models._invalidate_request_cache(rid)
    return rid


def ensure_login(rid) -> str:
    """The demo's owner login. DEMO_PASSWORD (the existing demo's) when it is
    set and passes the policy; otherwise a random password stored nowhere —
    set DEMO_PASSWORD and run again to sign in."""
    import secrets
    import models
    from auth import create_user, password_policy_error
    from werkzeug.security import generate_password_hash
    wanted = (os.getenv("DEMO_PASSWORD") or "").strip()
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()
        if row:
            other = conn.execute("SELECT restaurant_id FROM users WHERE id=?", (row["id"],)).fetchone()
            if int(other["restaurant_id"] or 0) != int(rid):
                raise RuntimeError(f"username {USERNAME!r} belongs to another restaurant")
            if wanted and not password_policy_error(wanted):
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(wanted), row["id"]))
                conn.commit()
                return "password = DEMO_PASSWORD"
            return "login kept"
    finally:
        conn.close()
    if wanted and not password_policy_error(wanted):
        create_user(rid, USERNAME, OWNER_EMAIL, wanted)
        return "created, password = DEMO_PASSWORD"
    create_user(rid, USERNAME, OWNER_EMAIL, secrets.token_urlsafe(24), generated=True)
    return "created with a random password (set DEMO_PASSWORD and re-run)"


# ── the POS history ─────────────────────────────────────────────────────────

def fill_pos(rid, archive_days=ARCHIVE_DAYS, history_days=HISTORY_DAYS):
    """Labor and daily sales for `history_days`, the ticket archive for `archive_days`, the POS's
    lists (roles, prices, catalog), comps and voids, dish sales."""
    import pos
    import pos_archive
    import rpower
    today = api.business_today()
    rpower.clear_caches()
    # 1. The long labor file: every punch and day of sales, as the nightly sync reads them.
    _say(f"labor + sales, {history_days} days")
    csv_str = rpower.build_shifts_csv(rid, days=history_days)
    rows = pos.save_synced_shifts(rid, csv_str, "rpower")
    _say(f"  {rows} shift rows")
    # 2. The ticket archive, oldest first.
    _say(f"ticket archive, {archive_days} days")
    for i in range(archive_days, 0, -1):
        day = today - timedelta(days=i)
        got = pos_archive.archive_day(rid, day)
        if i % 20 == 0:
            _say(f"  {got['date']}: {got['tickets']} checks, ${got['net_sales']:,.0f}")
    # 3. The POS's lists.
    for name, fn in (("catalog", pos_archive.sync_catalog), ("roles", pos_archive.sync_roles),
                     ("prices", pos_archive.sync_prices)):
        try:
            _say(f"{name}: {fn(rid)}")
        except Exception as e:
            _say(f"{name} FAILED: {e}")
    # 4. The nightly sync as the scheduler runs it (60 days, menu discovery, dish sales).
    _say(f"nightly sync: {pos.sync_restaurant(rid, trigger='demo_build')}")
    try:
        import loss_detection
        _say(f"loss sync: {loss_detection.sync(rid)}")
    except Exception as e:
        _say(f"loss sync FAILED: {e}")
    try:
        import inventory_ledger
        _say(f"menu discovery: {inventory_ledger.discover_menu_items(rid, days=60)}")
        _say(f"dish sales: {inventory_ledger.backfill_item_sales(rid, days=history_days)}")
    except Exception as e:
        _say(f"dish sales FAILED: {e}")


# ── settings an owner sets once ─────────────────────────────────────────────

def _hours_json(which):
    import json
    from demo_store import calendar as cal
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def fmt(h):
        h = h % 24
        return f"{(h % 12) or 12}:00{'am' if h < 12 else 'pm'}"
    return json.dumps({names[i]: fmt(cal.HOURS[i][0 if which == "open" else 1]) for i in range(7)})


def fill_settings(rid):
    import json
    import models
    rates = {job: v[1] for job, v in w.JOBS.items() if v[1]}
    models.update_restaurant(rid, {
        "open_times_json": _hours_json("open"), "close_times_json": _hours_json("close"),
        "hours_notes": "Kitchen until 11pm; late-night menu Fri-Sat until 1:30am. Brunch Sat-Sun 10am-3pm.",
        "role_rates_json": json.dumps(rates),
        "salaried_staff_json": json.dumps([{"name": "Priya Raman", "annual": 78000},
                                           {"name": "Maya Castellanos", "annual": 85000}]),
        "role_minimums_json": json.dumps({"Server": 2, "Bartender": 2, "Line Cook": 2, "Dishwasher": 1,
                                          "Manager": 1, "Host": 1}),
        "dsr_deadline_hour": 7, "dsr_late_night_hour": 22, "jurisdiction": "chicago",
        "section_count": 6, "daypart_split": 1, "daypart_boundary": "16:00",
    })
    from dsr import store as dsr_store
    for dep, cat in (("Food", "Food"), ("Beer", "Beer"), ("Liquor", "Liquor"), ("Wine", "Wine"),
                     ("Beverage", "NA Beverage"), ("Discounts", "Discounts"), ("Other", "Merchandise")):
        dsr_store.set_category(rid, dep, cat)
    models._invalidate_request_cache(rid)
    _say("settings set")


# ── the nightly report ─────────────────────────────────────────────────────

def fill_dsr(rid, nights=28, history_days=HISTORY_DAYS):
    """The Daily Sales Report for the last `nights` nights, built by the real pipeline (its
    delivery reaches no one: demo_store.is_simulated stops every send), and its figures for the
    year before (dsr.backfill, what the week, period and year views read)."""
    import models
    from dsr import backfill, pipeline
    r = models.get_restaurant(rid)
    got = backfill.backfill(r, days=history_days, max_days=history_days)
    _say(f"DSR figures backfilled: {got}")
    today = api.business_today()
    done = {}
    for i in range(nights, 0, -1):
        day = today - timedelta(days=i)
        try:
            out = pipeline.run_night(r, day, pipeline.TRIGGER_MANUAL, force=True)
            done[out.get("action")] = done.get(out.get("action"), 0) + 1
        except Exception as e:
            _say(f"  DSR {day} FAILED: {e}")
    _say(f"DSR nights: {done}")


# ── published schedules ────────────────────────────────────────────────────

def _clock(dt):
    return dt.strftime("%-I:%M %p")


def week_csv(week_start) -> tuple:
    """(schedule text, hours) of the week as it was planned — the roster the punches came from."""
    import schedule_rows
    from demo_store import day as D
    rows, hours = [], 0.0
    for s in sorted(D.week(week_start)["roster"], key=lambda x: (x["date"], x["start"], x["job"])):
        h = round((s["end"] - s["start"]).total_seconds() / 3600.0, 2)
        hours += h
        rows.append({"date": s["date"], "day": s["start"].strftime("%A"), "employee": s["name"], "role": s["job"],
                     "shift_start": _clock(s["start"]), "shift_end": _clock(s["end"]), "scheduled_hours": h,
                     "notes": "", "shift_id": schedule_rows.new_id() if hasattr(schedule_rows, "new_id") else ""})
    return schedule_rows.to_csv(rows), round(hours, 1)


def fill_schedules(rid, weeks=12, ahead=None):
    """The last `weeks` weeks and this one, published the Thursday before each began — and next
    week too once its Thursday has come (`ahead`, default: from Thursday on)."""
    import models
    import schedule_versions
    from demo_store import day as D
    today = api.business_today()
    this_week = D.week_of(today)
    conn = models.get_conn()
    try:
        have = {r["week_start"] for r in conn.execute(
            "SELECT week_start FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL", (rid,))}
    finally:
        conn.close()
    made = 0
    if ahead is None:
        ahead = 1 if today.weekday() >= 3 else 0
    for k in range(weeks, -1 - ahead, -1):
        ws = this_week - timedelta(weeks=k)
        if ws.isoformat() in have:
            continue
        text, hours = week_csv(ws)
        hid = models.save_schedule_history(rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), hours,
                                           round(hours * 1.02, 1), 28.0, text, [])
        stamp = (ws - timedelta(days=4)).isoformat() + " 20:15:00"
        conn = models.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            schedule_versions.store_week(conn, rid, hid, published=text,
                                         sets=("published_at=?", "published_by=?"), args=(stamp, "Marcus Bell"),
                                         allow_overlaps=True)
            conn.execute("UPDATE schedule_history SET generated_at=? WHERE id=?",
                         ((ws - timedelta(days=5)).isoformat() + " 14:40:00", hid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        made += 1
    _say(f"schedules: {made} weeks published")


# ── purchasing: Fintech (bar), Back Office (kitchen), the recipe book ───────

def fill_purchasing(rid, days=HISTORY_DAYS):
    import backoffice_ap
    import backoffice_recipes
    import fintech_invoices
    from demo_store import purchasing as P
    today = api.business_today()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    # Bar: one Fintech export file a month, as Fintech emails them.
    invs = P.bar_invoices(start, end)
    by_month = {}
    for inv in invs:
        by_month.setdefault(inv["date"].strftime("%Y-%m"), []).append(inv)
    stored = 0
    for month, rows in sorted(by_month.items()):
        res = fintech_invoices.ingest_file(rid, f"halsted_rye_fintech_{month}.csv", P.fintech_csv(rows).encode())
        stored += int(res.get("invoices") or 0)
    _say(f"Fintech: {len(invs)} invoices generated, {stored} stored")
    # Kitchen: the Chart of Accounts, every invoice by vendor, the itemized ones' lines.
    backoffice_ap.save_coa(rid, [{"gl": g, "name": n, "subtype": s, "category": backoffice_ap.gl_category(s, n)}
                                 for g, n, s in P.COA])
    food = P.food_invoices(start, end)
    vendors = {}
    for inv in food:
        gls = {}
        for l in inv["lines"]:
            gls[l["gl"]] = gls.get(l["gl"], 0.0) + l["ext_amount"]
        main = max(gls.items(), key=lambda kv: kv[1])[0]
        vendors.setdefault(inv["vendor"], []).append({
            "entry_kind": "invoice", "invoice_number": inv["number"], "itemized": bool(inv["lines"][0]["item_code"]),
            "date": inv["date"].isoformat(), "amount": round(sum(gls.values()), 2), "gl": main})
    backoffice_ap.save_invoices(rid, {"vendors": [{"vendor": v, "total": round(sum(r["amount"] for r in rows), 2),
                                                   "rows": rows} for v, rows in vendors.items()]})
    lines = 0
    for inv in food:
        if inv["lines"][0]["item_code"]:
            lines += backoffice_ap.save_lines(rid, inv["vendor"], inv["number"], inv["lines"],
                                              invoice_date=inv["date"].isoformat())
    _say(f"Back Office: {len(food)} invoices, {lines} lines")
    got = backoffice_recipes.load(rid, P.recipe_book(today), source="Back Office recipe export")
    _say(f"recipe book: {got}")


# ── the bar: drink matches, cocktail specs, month-end counts ───────────────

# Drinks left for the owner to match (new this month), so Matching has work to show.
UNMATCHED = {"Revolution Seasonal Draft", "Nojito", "Lemon Drop Shot", "Green Tea Shot", "Paloma"}


def _pkey(prod):
    from bar.classify import product_key
    p = w.BAR_PRODUCTS[prod]
    return product_key(w.DISTRIBUTORS[p[0]][0], p[1], p[2])


def fill_bar(rid):
    import models
    from bar import counts, margins
    from bar.variance import init_bar_variance
    init_bar_variance()
    mapped = specs = 0
    cocktails = []
    for m in w.menu():
        if m["kind"] != "drink" or not m.get("pours") or m["name"] in UNMATCHED:
            continue
        if len(m["pours"]) > 1:
            cocktails.append(m)
            continue
        prod, oz = m["pours"][0]
        p = w.BAR_PRODUCTS[prod]
        sold_whole = p[4] == "CS" and oz == p[3]
        margins.set_drink_map(rid, "rpower", m["mid"], _pkey(prod), None if sold_whole else oz)
        mapped += 1
    conn = models.get_conn()
    try:
        conn.execute("DELETE FROM bar_drink_specs WHERE restaurant_id=? AND source='demo_store'", (rid,))
        for m in cocktails:
            for prod, oz in m["pours"]:
                conn.execute(
                    "INSERT INTO bar_drink_specs (restaurant_id, provider, item_name, item_id, ingredient, product_key, "
                    "oz, status, source, recipe, note, set_by, set_at) VALUES (?,?,?,?,?,?,?,'confirmed','demo_store',?,"
                    "NULL,NULL,datetime('now')) ON CONFLICT(restaurant_id, provider, item_name, ingredient) DO UPDATE SET "
                    "product_key=excluded.product_key, oz=excluded.oz, status='confirmed'",
                    (rid, "rpower", m["name"], m["mid"], w.BAR_PRODUCTS[prod][2].title(), _pkey(prod), oz, m["name"]))
                specs += 1
        conn.commit()
    finally:
        conn.close()
    # Month-end counts for the last six months, by category value.
    import random
    today = api.business_today()
    first = today.replace(day=1)
    saved = 0
    ends, cur = [], first
    for _k in range(6):
        cur = cur - timedelta(days=1)
        ends.append(cur)
        cur = cur.replace(day=1)
    for month_end in sorted(ends):
        rng = random.Random(f"hr-count|{month_end}")
        vals = {"beer": round(4600 * rng.uniform(0.88, 1.15), 2), "liquor": round(11800 * rng.uniform(0.92, 1.08), 2),
                "wine": round(3300 * rng.uniform(0.85, 1.15), 2)}
        counts.save_count(rid, month_end.isoformat(), vals, note="Month-end count", today=today)
        saved += 1
    _say(f"bar: {mapped} drinks matched, {specs} cocktail spec lines, {saved} month-end counts")


# ── reviews ────────────────────────────────────────────────────────────────

def fill_reviews(rid, days=HISTORY_DAYS):
    import json
    import models
    from demo_store import reviews as R
    today = api.business_today()
    rows = R.generate(today - timedelta(days=days), today - timedelta(days=1))
    objs = [models.Review(restaurant_id=rid, platform="google", external_id=x["external_id"], author=x["author"],
                          rating=x["rating"], text=x["text"], review_date=x["review_date"],
                          fetched_at=x["review_date"], sentiment=x["sentiment"], categories=x["categories"],
                          summary=x["summary"], urgency=x["urgency"], draft_response=x["draft_response"],
                          response_status=x["response_status"], posted_at=x["posted_at"], processed=True)
            for x in rows]
    new, _ = models.save_reviews(objs)
    # save_reviews keeps an existing row as it was: bring every demo review to its generated state.
    conn = models.get_conn()
    try:
        for x in rows:
            conn.execute("UPDATE reviews SET sentiment=?, categories=?, summary=?, urgency=?, draft_response=?, "
                         "response_status=?, posted_at=?, approved_at=?, processed=1 WHERE restaurant_id=? AND external_id=?",
                         (x["sentiment"], json.dumps(x["categories"]), x["summary"], x["urgency"],
                          x["draft_response"], x["response_status"], x["posted_at"], x["posted_at"], rid,
                          x["external_id"]))
        conn.commit()
    finally:
        conn.close()
    avg = sum(x["rating"] for x in rows) / max(1, len(rows))
    models.update_restaurant(rid, {"reviews_live": 1})
    _say(f"reviews: {len(rows)} ({new} new), average {avg:.2f}")


# ── the kitchen count: ingredients, recipes, deliveries, counts, waste ──────

_UNIT = {"Pound": "lb", "Ounce": "oz", "Fluid Ounce": "fl oz", "Each": "each"}
_CAT = {"FOOD - MEAT": "Meat", "FOOD - POULTRY": "Poultry", "FOOD - SEAFOOD": "Seafood", "FOOD - PRODUCE": "Produce",
        "FOOD - DAIRY": "Dairy", "FOOD - BAKERY": "Bakery", "FOOD - DRY GOODS": "Dry goods",
        "FOOD - FROZEN": "Frozen"}
# Where the last count lands: a few short before the weekend, a few bought well past par.
_SHORT = {"avocado": 0.25, "wings": 0.35, "brioche": 0.3}
_OVER = {"strip": 1.9, "salmon": 1.7, "cheesecake": 2.1}
_WASTE = [("romaine", "spoiled"), ("tomato", "spoiled"), ("avocado", "browned"), ("cilantro", "wilted"),
          ("brisket", "over-prepped"), ("wings", "dropped"), ("cream", "expired"), ("greens", "spoiled"),
          ("salmon", "past its date"), ("short_rib", "over-prepped"), ("beef", "over-prepped"),
          ("brioche", "stale"), ("fries", "dropped"), ("breast", "over-prepped")]


def fill_kitchen(rid, weeks=6):
    import json
    import random
    import models
    import inventory_ledger as il
    from demo_store import purchasing as P
    today = api.business_today()
    start = today - timedelta(days=7 * weeks + 2)
    days = [start + timedelta(days=i) for i in range((today - start).days)]
    use = {k: 0.0 for k in w.FOOD_ITEMS}
    daily = {}
    for d in days:
        u = P.usage(d)["food"]
        daily[d] = u
        if d >= today - timedelta(days=28):
            for k, v in u.items():
                use[k] += v / 28.0
    conn = models.get_conn()
    try:
        conn.execute("DELETE FROM ingredient_stock_events WHERE restaurant_id=?", (rid,))
        conn.execute("DELETE FROM recipe_ingredients WHERE menu_item_id IN (SELECT id FROM menu_items WHERE restaurant_id=?)",
                     (rid,))
        conn.execute("DELETE FROM ingredients WHERE restaurant_id=?", (rid,))
        conn.execute("DELETE FROM inventory_history WHERE restaurant_id=?", (rid,))
        ids = {}
        for k, (name, gl, vendor, sku, pu, per_pack, pack_unit, price) in w.FOOD_ITEMS.items():
            unit_cost = round(price / per_pack, 4)
            par = round(max(per_pack * 0.5, use[k] * 4.0), 1)
            cur = conn.execute(
                "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, case_size, "
                "current_stock, avg_daily_usage, last_order_qty, waste_last_week, last_recount_at, supplier_name, "
                "supplier_email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, name, _CAT[gl], _UNIT[pack_unit], par, unit_cost, per_pack, par, round(use[k], 3),
                 per_pack, 0, None, vendor, "orders@" + vendor.lower().replace(" ", "").replace("&", "") + ".example"))
            ids[k] = cur.lastrowid
        # Recipes on the POS's own items (discover_menu_items made them, keyed by RPOWER's id).
        menu_rows = {r["toast_guid"]: r["id"] for r in conn.execute(
            "SELECT id, toast_guid FROM menu_items WHERE restaurant_id=? AND toast_guid IS NOT NULL", (rid,))}
        linked = 0
        for m in w.menu():
            if m["kind"] != "food" or not m.get("recipe") or m["mid"] not in menu_rows:
                continue
            conn.execute("UPDATE menu_items SET sell_price=? WHERE id=?", (m["price"], menu_rows[m["mid"]]))
            for k, q in P.explode(tuple(tuple(x) for x in m["recipe"])).items():
                conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                             (menu_rows[m["mid"]], ids[k], round(q, 5)))
            linked += 1
        # Deliveries, from the kitchen invoices.
        by_sku = {v[3]: k for k, v in w.FOOD_ITEMS.items()}
        got = {}
        for inv in P.food_invoices(start, today - timedelta(days=1)):
            for l in inv["lines"]:
                k = by_sku.get(l.get("item_code"))
                if not k:
                    continue
                per_pack = w.FOOD_ITEMS[k][5]
                catch = abs(float(l["qty"]) - round(float(l["qty"]))) > 0.01
                qty = float(l["qty"]) if catch else float(l["qty"]) * per_pack
                got.setdefault(inv["date"], {}).setdefault(k, 0.0)
                got[inv["date"]][k] += qty
                conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                             "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                             (rid, ids[k], "receiving", round(qty, 2), inv["date"].isoformat(), "invoice", inv["number"]))
        # Weekly counts (Monday morning) and logged waste, on a running stock.
        stock = {k: round(use[k] * 5.5, 2) for k in w.FOOD_ITEMS}
        conn.execute("DELETE FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='recount'", (rid,))
        snaps = []
        wasted = {}

        def week_key(d):
            return d - timedelta(days=d.weekday())
        for d in days:
            rng = random.Random(f"hr-kitchen|{d}")
            for k, q in (got.get(d) or {}).items():
                stock[k] += q
            for k, q in daily[d].items():
                stock[k] = max(0.0, stock[k] - q)
            for _w in range(1 + (rng.random() < 0.6) + (rng.random() < 0.3)):
                k, why = rng.choice(_WASTE)
                q = round(min(stock[k], use[k] * rng.uniform(0.25, 0.9)), 2)
                if q > 0:
                    stock[k] -= q
                    wasted.setdefault(week_key(d), []).append((k, q))
                    conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                                 "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                                 (rid, ids[k], "waste", q, d.isoformat(), "logged", why))
            if d.weekday() == 0 or d == days[-1]:
                last = d == days[-1]
                items, value = [], 0.0
                for k in w.FOOD_ITEMS:
                    counted = stock[k] * rng.uniform(0.93, 1.01)
                    if last and k in _SHORT:
                        counted = use[k] * _SHORT[k] * 7
                    if last and k in _OVER:
                        counted = float(conn.execute("SELECT par_level FROM ingredients WHERE id=?", (ids[k],))
                                        .fetchone()[0]) * _OVER[k]
                    counted = round(max(0.0, counted), 2)
                    stock[k] = counted
                    conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                                 "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                                 (rid, ids[k], "recount", counted, d.isoformat(), "count", "Monday kitchen count"))
                    name, gl, _v, _s, _pu, per_pack, pack_unit, price = w.FOOD_ITEMS[k]
                    uc = price / per_pack
                    value += counted * uc
                    items.append({"item": name, "category": _CAT[gl], "unit": _UNIT[pack_unit], "unit_cost": round(uc, 4),
                                  "par_level": round(max(per_pack * 0.5, use[k] * 4.0), 1), "current_stock": counted,
                                  "avg_daily_usage": round(use[k], 3), "last_order_qty": per_pack, "waste_last_week": 0})
                wk = wasted.get(week_key(d - timedelta(days=1)), [])
                cost = {}
                for k, q in wk:
                    cost[k] = cost.get(k, 0.0) + q * w.FOOD_ITEMS[k][7] / w.FOOD_ITEMS[k][5]
                for it in items:
                    key = next(k for k, v in w.FOOD_ITEMS.items() if v[0] == it["item"])
                    it["waste_last_week"] = round(sum(q for k, q in wk if k == key), 2)
                snaps.append((d, items, value, round(sum(cost.values()), 2),
                              [w.FOOD_ITEMS[k][0] for k, _c in sorted(cost.items(), key=lambda kv: -kv[1])[:4]]))
        for d, items, value, waste_cost, top in snaps:
            conn.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, source) "
                         "VALUES (?,?,?,?,?,?)", (rid, json.dumps({"inventory_value": round(value, 2),
                                                                   "total_waste_cost": waste_cost, "top_items": top}),
                                                  d.isoformat(), json.dumps(items), round(value, 2), "count"))
        conn.commit()
    finally:
        conn.close()
    # The POS depletion, through the ledger's own nightly path, then every rollup.
    for d in days:
        try:
            il.compute_daily_depletion(rid, d)
        except Exception as e:
            _say(f"  depletion {d} FAILED: {e}")
    for k, iid in ids.items():
        il.recompute_rollups(rid, iid)
    _say(f"kitchen: {len(ids)} ingredients, {linked} dishes with recipes, {len(snaps)} counts")


# ── people: ratings, closers, availability ─────────────────────────────────

_RATED = {"Marcus Bell": 5, "Tessa Okonkwo": 4, "Grant Holloway": 4, "Priya Raman": 5, "Luis Ortega": 5,
          "Andre Kimura": 4, "Rafael Montoya": 5, "Keisha Dunmore": 4, "Tomas Villanueva": 4, "Brandon Szymanski": 3,
          "Hector Galindo": 4, "Jamal Pritchett": 3, "Ana Sepulveda": 4, "Devon Marsh": 2, "Rosa Arellano": 4,
          "Edgar Fuentes": 4, "Jordan Reyes": 3, "Kayla Brennan": 5, "Sofia Lindqvist": 4, "Tyler Novak": 4,
          "Brianna Coleman": 3, "Nate Ferraro": 4, "Aisha Mahmoud": 4, "Caleb Ostrowski": 3, "Mia Delgado": 3,
          "Danielle Wu": 5, "Chris Kowalczyk": 5, "Jasmine Thornton": 4, "Ryan Mulcahy": 4, "Elena Petrova": 4,
          "Derek Lam": 3, "Camille Dubois": 4, "Omar Haddad": 4, "Lily Nakamura": 4, "Javier Solis": 4}
_CLOSERS = ["Marcus Bell", "Tessa Okonkwo", "Grant Holloway", "Chris Kowalczyk", "Danielle Wu", "Ryan Mulcahy"]


def fill_people(rid):
    import models
    n = 0
    for name, score in _RATED.items():
        try:
            models.set_capability(rid, name, score=score, updated_by="Maya Castellanos")
            n += 1
        except Exception as e:
            _say(f"  rating {name} FAILED: {e}")
    for name in _CLOSERS:
        try:
            models.set_capability(rid, name, attribute="can_close", flag=True, updated_by="Maya Castellanos")
        except Exception as e:
            _say(f"  closer {name} FAILED: {e}")
    _say(f"people: {n} rated, {len(_CLOSERS)} closers")


# ── what the nightly learners know: games, features, DNA, value ────────────

def fill_intel(rid):
    """The passes the scheduler runs every night, run now over the store's history: its game
    follows and what each night's events did, the weekly feature rows and Restaurant DNA, the
    value point. The showcase learns only from itself (models.learns_for_itself); every demo is
    left out of what teaches other restaurants."""
    import models
    import event_memory
    from event_intel import engine as ev_engine, store as ev_store
    from intelligence import dna, features
    r = models.get_restaurant(rid)
    today = api.business_today() + timedelta(days=1)
    try:
        ev_store.load_bundled()
        _say(f"follows: {ev_engine.ensure_follows(r)}")
        _say(f"events: {ev_engine.sync_restaurant(r)}")
    except Exception as e:
        _say(f"events FAILED: {e}")
    for _ in range(8):
        try:
            got = event_memory.remember_restaurant(r)
            _say(f"event memory: {got.get('recorded')} nights")
            if not got.get("recorded"):
                break
        except Exception as e:
            _say(f"event memory FAILED: {e}")
            break
    weeks = features.weeks_to_backfill(rid, today=today, max_weeks=60)
    for wk in weeks:
        try:
            features.backfill_week(rid, wk)
        except Exception as e:
            _say(f"  features {wk} FAILED: {e}")
    try:
        f = features.compute_and_store(rid, today=today)
        dna.compute_and_store(rid, today=today, features=f, restaurant=r, norms=dna.platform_norms())
        _say(f"features: {len(weeks)} past weeks + this week, DNA written")
    except Exception as e:
        _say(f"features/DNA FAILED: {e}")
    try:
        import value_delivered as vd
        vd.record_value_snapshot(rid, vd.compute_total_value_delivered(rid))
        _say("value point written")
    except Exception as e:
        _say(f"value FAILED: {e}")


# ── last: what the build itself wrote is history, not news ─────────────────

def fill_finish(rid):
    """The settings, menu and prices the build wrote are the store's setup from when it opened:
    their change-log rows are dated to the start of its history, so no screen reads "a sync added
    138 dishes today"."""
    import models
    start = (api.business_today() - timedelta(days=HISTORY_DAYS)).isoformat() + " 09:00:00"
    conn = models.get_conn()
    try:
        since = _STARTED["at"] or "9999"
        n = conn.execute("UPDATE change_log SET changed_at=? WHERE restaurant_id=? AND changed_at>=?",
                         (start, rid, since)).rowcount
        conn.commit()
    finally:
        conn.close()
    models._invalidate_request_cache(rid)
    _say(f"finish: {n} setup changes dated {start[:10]}")


# ── marketing: posts, guests who opted in, past texts ───────────────────────

_POSTS = [("Bears Sunday: $26 Old Style towers and 50-wing platters all game", "game_day"),
          ("Brunch is back on the patio — chicken & waffles and $9 mimosas", "brunch"),
          ("New on draft: Revolution's fall seasonal", "new_item"),
          ("Cubs home opener: doors at 10am, Malört shots to celebrate", "game_day"),
          ("Meet Danielle, behind our bar since 2022", "team"),
          ("The Halsted Smash: two patties, Halsted sauce, done right", "menu"),
          ("Happy hour 3-6pm weekdays: $5 Millers, $9 house margaritas", "offer"),
          ("Italian beef, dipped, with hot giardiniera. Chicago.", "menu"),
          ("Market Days weekend — patio open 10am to close", "event"),
          ("Pride on Halsted: open early, all weekend, all love", "event"),
          ("Wing Wednesday: 10 for $12 at the bar", "offer"),
          ("Thursday Night Football + $7 Anti-Hero pints", "game_day")]


def _posts(conn, rid, start, end) -> int:
    """Three posts a week (Monday, Wednesday, Friday), each date's post the same whenever it is written."""
    import random
    n = 0
    d = start
    while d <= end:
        if d.weekday() in (0, 2, 4):
            rng = random.Random(f"hr-post|{d}")
            topic, kind = _POSTS[d.toordinal() % len(_POSTS)]
            plat = "instagram" if rng.random() < 0.65 else "facebook"
            pid = f"hr-{plat}-{d:%Y%m%d}"
            if not conn.execute("SELECT 1 FROM marketing_content_log WHERE restaurant_id=? AND post_id=?",
                                (rid, pid)).fetchone():
                reach = int(rng.uniform(900, 4200) * (1.6 if kind == "game_day" else 1.0))
                at = f"{d.isoformat()} {rng.choice(['11:30', '15:00', '17:45', '19:10'])}:00"
                conn.execute(
                    "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, created_at, reach, "
                    "impressions, engaged, likes, comments, shares, post_id, post_platform, posted_at, occasion, "
                    "post_kind, origin, metrics_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, f"{plat}_post", topic, at, reach, int(reach * rng.uniform(1.3, 1.9)),
                     int(reach * rng.uniform(0.04, 0.09)), int(reach * rng.uniform(0.03, 0.07)), int(rng.uniform(2, 30)),
                     int(rng.uniform(0, 12)), pid, plat, at, kind, kind, "demo_store", at))
                n += 1
        d += timedelta(days=1)
    return n


def fill_marketing(rid, weeks=16):
    import random
    import models
    today = api.business_today()
    conn = models.get_conn()
    try:
        n = _posts(conn, rid, today - timedelta(days=weeks * 7), today - timedelta(days=1))
        # Guests who opted in to texts at the host stand (fictional 555-01xx numbers).
        conn.execute("DELETE FROM guest_contacts WHERE restaurant_id=?", (rid,))
        from demo_store.reviews import FIRST, LAST_INITIAL
        guests = 0
        for i, area in enumerate(("312", "773", "872")):
            for j in range(100):
                rng = random.Random(f"hr-guest|{area}|{j}")
                if rng.random() < 0.25:
                    continue
                joined = today - timedelta(days=rng.randint(10, 380))
                conn.execute("INSERT OR IGNORE INTO guest_contacts (restaurant_id, name, phone, consent, consent_at, "
                             "unsubscribed, last_visit, created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (rid, f"{rng.choice(FIRST)} {rng.choice(LAST_INITIAL)}.", f"+1{area}55501{j:02d}", 1,
                              joined.isoformat() + " 19:00:00", 1 if rng.random() < 0.04 else 0,
                              (today - timedelta(days=rng.randint(1, 60))).isoformat(), joined.isoformat() + " 19:00:00"))
                guests += 1
        conn.execute("DELETE FROM guest_campaigns WHERE restaurant_id=?", (rid,))
        texts = [("Bears at noon Sunday — towers $26, wings 50 for $72. Save your seat: reply YES", 34, 41),
                 ("Cubs home tonight! Pregame happy hour till 6. Reply STOP to opt out", 21, 28),
                 ("Brunch on the patio this weekend, mimosas $9. Reply STOP to opt out", 52, 31),
                 ("Thursday Night Football + $7 Anti-Hero pints. Reply STOP to opt out", 9, 19)]
        for k, (msg, ago, matched) in enumerate(texts):
            conn.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, created_at, "
                         "visits_matched, attribution_through) VALUES (?,?,?,?,?,?,?)",
                         (rid, msg, int(guests * 0.92), 2, (today - timedelta(days=ago)).isoformat() + " 15:30:00",
                          matched, (today - timedelta(days=1)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    _say(f"marketing: {n} posts, {guests} opted-in guests, 4 past texts")


# ── the neighborhood: fictional competitors and the store's own rating ──────

COMPETITORS = [("hr-comp-seventh", "Seventh Inning Social", 4.2, 1840, 2), ("hr-comp-northbound", "Northbound Alehouse", 4.4, 960, 2),
               ("hr-comp-bleacher", "Bleacher & Brine", 4.0, 2310, 2), ("hr-comp-cornerkick", "Corner Kick Tavern", 4.3, 610, 1),
               ("hr-comp-ashland", "Ashland Ember Grill", 4.5, 1120, 2)]


def fill_neighborhood(rid, months=13):
    import random
    import models
    today = api.business_today()
    conn = models.get_conn()
    try:
        conn.execute("DELETE FROM competitor_snapshots WHERE restaurant_id=?", (rid,))
        conn.execute("DELETE FROM competitor_rating_monthly WHERE restaurant_id=?", (rid,))
        conn.execute("DELETE FROM own_rating_history WHERE restaurant_id=?", (rid,))
        conn.execute("DELETE FROM market_events WHERE restaurant_id=?", (rid,))
        for pid, name, rating, count, price in COMPETITORS:
            rng = random.Random(f"hr-comp|{pid}")
            r, c = rating - 0.1, int(count * 0.82)
            for k in range(months, -1, -1):
                d = (today.replace(day=1) - timedelta(days=30 * k)).replace(day=1)
                c += int(rng.uniform(8, 30))
                r = round(min(4.8, max(3.6, r + rng.gauss(0.008, 0.02))), 2)
                conn.execute("INSERT INTO competitor_rating_monthly (restaurant_id, place_id, month, name, rating, "
                             "review_count, captured_at) VALUES (?,?,?,?,?,?,?)",
                             (rid, pid, d.strftime("%Y-%m"), name, round(r, 1), c, d.isoformat() + " 06:00:00"))
            conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, review_count, "
                         "price_level, match_basis, captured_at) VALUES (?,?,?,?,?,?,?,?)",
                         (rid, pid, name, round(r, 1), c, price, "nearby sports bar", today.isoformat() + " 06:00:00"))
            conn.execute("INSERT INTO market_events (restaurant_id, place_id, name, kind, from_rating, to_rating, "
                         "review_count, observed_on, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (rid, pid, name, "tracked", None, round(r, 1), c, today.isoformat(), today.isoformat() + " 06:00:00"))
        rows = conn.execute("SELECT review_date, rating FROM reviews WHERE restaurant_id=? ORDER BY review_date",
                            (rid,)).fetchall()
        base_n, base_sum = 812, 812 * 4.5
        by_week = {}
        for x in rows:
            d = date.fromisoformat(str(x["review_date"])[:10])
            by_week.setdefault(d - timedelta(days=d.weekday()), []).append(x["rating"])
        n, s = base_n, base_sum
        for wk in sorted(by_week):
            n += len(by_week[wk])
            s += sum(by_week[wk])
            conn.execute("INSERT INTO own_rating_history (restaurant_id, week, rating, review_count, source, recorded_at) "
                         "VALUES (?,?,?,?,?,?)", (rid, wk.isoformat(), round(s / n, 1), n, "google", wk.isoformat() + " 06:00:00"))
        conn.commit()
    finally:
        conn.close()
    _say(f"neighborhood: {len(COMPETITORS)} competitors, {len(by_week)} weeks of own rating")


# ── every night: the store moves on a day ──────────────────────────────────
#
# The POS side needs nothing here — the nightly sync, the ticket archive and
# the Daily Sales Report read the simulated store like any RPOWER store. What
# arrives from elsewhere at a real restaurant is written here, from the same
# generators the build used (each day's records are the same whenever they
# are generated, so a missed night is caught up and a re-run changes nothing):
# the week's distributor and kitchen invoices, new reviews and replies, the
# kitchen's deliveries, waste and Monday count, posts, the coming week's
# schedule on Thursday, the month-end bar count. Archive days past the
# store's history are dropped, so the account stays the same size.

REFRESH_DAYS = 10


def _save_food(rid, food):
    import backoffice_ap
    vendors = {}
    for inv in food:
        gls = {}
        for l in inv["lines"]:
            gls[l["gl"]] = gls.get(l["gl"], 0.0) + l["ext_amount"]
        vendors.setdefault(inv["vendor"], []).append({
            "entry_kind": "invoice", "invoice_number": inv["number"], "itemized": bool(inv["lines"][0]["item_code"]),
            "date": inv["date"].isoformat(), "amount": round(sum(gls.values()), 2),
            "gl": max(gls.items(), key=lambda kv: kv[1])[0]})
    if vendors:
        backoffice_ap.save_invoices(rid, {"vendors": [{"vendor": v, "total": round(sum(r["amount"] for r in rows), 2),
                                                       "rows": rows} for v, rows in vendors.items()]})
    n = 0
    for inv in food:
        if inv["lines"][0]["item_code"]:
            n += backoffice_ap.save_lines(rid, inv["vendor"], inv["number"], inv["lines"],
                                          invoice_date=inv["date"].isoformat())
    return n


def _refresh_reviews(rid, start, end) -> int:
    import json
    import models
    from demo_store import reviews as R
    rows = R.generate(start, end)
    objs = [models.Review(restaurant_id=rid, platform="google", external_id=x["external_id"], author=x["author"],
                          rating=x["rating"], text=x["text"], review_date=x["review_date"], fetched_at=x["review_date"],
                          sentiment=x["sentiment"], categories=x["categories"], summary=x["summary"],
                          urgency=x["urgency"], draft_response=x["draft_response"],
                          response_status=x["response_status"], posted_at=x["posted_at"], processed=True)
            for x in rows]
    new, _ = models.save_reviews(objs)
    conn = models.get_conn()
    try:
        for x in rows:
            # Maya answers in a few days: a reply the generator now calls posted is posted — unless
            # someone acted on that review here already.
            conn.execute("UPDATE reviews SET sentiment=?, categories=?, summary=?, urgency=?, processed=1, "
                         "draft_response=COALESCE(draft_response, ?), response_status=?, posted_at=?, approved_at=? "
                         "WHERE restaurant_id=? AND external_id=? AND posted_at IS NULL "
                         "AND response_status IN ('pending', 'drafted')",
                         (x["sentiment"], json.dumps(x["categories"]), x["summary"], x["urgency"], x["draft_response"],
                          x["response_status"], x["posted_at"], x["posted_at"], rid, x["external_id"]))
        conn.commit()
    finally:
        conn.close()
    return new


def _refresh_kitchen(rid, start, end) -> dict:
    """Deliveries, POS depletion, logged waste and the Monday count for the days after the ledger's last."""
    import json
    import random
    import models
    import inventory_ledger as il
    from demo_store import purchasing as P
    conn = models.get_conn()
    try:
        ids = {}
        for r in conn.execute("SELECT id, name FROM ingredients WHERE restaurant_id=? AND COALESCE(is_active,1)=1",
                              (rid,)).fetchall():
            ids[r["name"]] = r["id"]
        last = conn.execute("SELECT max(event_date) d FROM ingredient_stock_events WHERE restaurant_id=? "
                            "AND source IN ('invoice', 'logged', 'count')", (rid,)).fetchone()["d"]
    finally:
        conn.close()
    if not ids or not last:
        return {"skipped": "no kitchen yet"}
    key_of = {v[0]: k for k, v in w.FOOD_ITEMS.items()}
    by_sku = {v[3]: k for k, v in w.FOOD_ITEMS.items()}
    first = max(start, date.fromisoformat(str(last)[:10]) + timedelta(days=1))
    days = [first + timedelta(days=i) for i in range((end - first).days + 1)]
    invoices = {}
    for inv in P.food_invoices(first, end):
        invoices.setdefault(inv["date"], []).append(inv)
    counted = 0
    for d in days:
        conn = models.get_conn()
        try:
            for inv in invoices.get(d, []):
                for l in inv["lines"]:
                    k = by_sku.get(l.get("item_code"))
                    if not k or w.FOOD_ITEMS[k][0] not in ids:
                        continue
                    catch = abs(float(l["qty"]) - round(float(l["qty"]))) > 0.01
                    qty = float(l["qty"]) if catch else float(l["qty"]) * w.FOOD_ITEMS[k][5]
                    iid = ids[w.FOOD_ITEMS[k][0]]
                    if not conn.execute("SELECT 1 FROM ingredient_stock_events WHERE restaurant_id=? AND ingredient_id=? "
                                        "AND event_type='receiving' AND note=?", (rid, iid, inv["number"])).fetchone():
                        conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                                     "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                                     (rid, iid, "receiving", round(qty, 2), d.isoformat(), "invoice", inv["number"]))
            conn.commit()
        finally:
            conn.close()
        try:
            il.compute_daily_depletion(rid, d)
        except Exception as e:
            _say(f"  depletion {d} FAILED: {e}")
        conn = models.get_conn()
        try:
            rng = random.Random(f"hr-kitchen|{d}")
            if not conn.execute("SELECT 1 FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='waste' "
                                "AND source='logged' AND event_date=?", (rid, d.isoformat())).fetchone():
                for _w in range(1 + (rng.random() < 0.6) + (rng.random() < 0.3)):
                    k, why = rng.choice(_WASTE)
                    iid = ids.get(w.FOOD_ITEMS[k][0])
                    row = conn.execute("SELECT avg_daily_usage, current_stock FROM ingredients WHERE id=?", (iid,)).fetchone()
                    q = round(min(float(row["current_stock"] or 0), float(row["avg_daily_usage"] or 0) * rng.uniform(0.25, 0.9)), 2)
                    if q > 0:
                        conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                                     "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                                     (rid, iid, "waste", q, d.isoformat(), "logged", why))
            conn.commit()
        finally:
            conn.close()
        for iid in ids.values():
            il.recompute_rollups(rid, iid)
        if d.weekday() == 0:
            conn = models.get_conn()
            try:
                items, value = [], 0.0
                for r in conn.execute("SELECT * FROM ingredients WHERE restaurant_id=? AND COALESCE(is_active,1)=1",
                                      (rid,)).fetchall():
                    q = round(max(0.0, float(r["current_stock"] or 0) * rng.uniform(0.95, 1.01)), 2)
                    conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, "
                                 "event_date, source, note) VALUES (?,?,?,?,?,?,?)",
                                 (rid, r["id"], "recount", q, d.isoformat(), "count", "Monday kitchen count"))
                    value += q * float(r["unit_cost"] or 0)
                    items.append({"item": r["name"], "category": r["category"], "unit": r["unit"],
                                  "unit_cost": r["unit_cost"], "par_level": r["par_level"], "current_stock": q,
                                  "avg_daily_usage": r["avg_daily_usage"], "last_order_qty": r["last_order_qty"],
                                  "waste_last_week": r["waste_last_week"]})
                wk = conn.execute("SELECT e.qty, i.unit_cost, i.name FROM ingredient_stock_events e JOIN ingredients i "
                                  "ON i.id=e.ingredient_id WHERE e.restaurant_id=? AND e.event_type='waste' AND "
                                  "e.event_date>=? AND e.event_date<?",
                                  (rid, (d - timedelta(days=7)).isoformat(), d.isoformat())).fetchall()
                cost = {}
                for x in wk:
                    cost[x["name"]] = cost.get(x["name"], 0.0) + float(x["qty"]) * float(x["unit_cost"] or 0)
                conn.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, "
                             "source) VALUES (?,?,?,?,?,?)",
                             (rid, json.dumps({"inventory_value": round(value, 2),
                                               "total_waste_cost": round(sum(cost.values()), 2),
                                               "top_items": [n for n, _c in sorted(cost.items(), key=lambda kv: -kv[1])[:4]]}),
                              d.isoformat(), json.dumps(items), round(value, 2), "count"))
                conn.commit()
                counted += 1
            finally:
                conn.close()
            for iid in ids.values():
                il.recompute_rollups(rid, iid)
    return {"days": len(days), "counts": counted}


def _prune(rid) -> int:
    import models
    cutoff = (api.business_today() - timedelta(days=HISTORY_DAYS + 30)).isoformat()
    conn = models.get_conn()
    n = 0
    try:
        for t in ("pos_ticket_lines", "pos_tickets", "pos_punches", "pos_payments", "pos_payouts", "pos_archive_days"):
            try:
                n += conn.execute(f"DELETE FROM {t} WHERE restaurant_id=? AND business_date<?", (rid, cutoff)).rowcount
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return n


def refresh(rid, today=None) -> dict:
    """One night's upkeep for the showcase (module note above). Every part is independent: one
    failing is reported and the rest still run."""
    import fintech_invoices
    from bar import counts
    from demo_store import purchasing as P
    today = today or api.business_today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=REFRESH_DAYS)
    out, failed = {}, []

    def step(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            failed.append(name)
            out[name] = f"FAILED: {e}"

    step("fintech", lambda: fintech_invoices.ingest_file(
        rid, f"halsted_rye_fintech_{end:%Y%m%d}.csv", P.fintech_csv(P.bar_invoices(start, end)).encode()).get("status"))
    step("back_office", lambda: _save_food(rid, P.food_invoices(start, end)))
    step("reviews", lambda: _refresh_reviews(rid, start, end))
    step("kitchen", lambda: _refresh_kitchen(rid, start, end))
    step("schedules", lambda: fill_schedules(rid, weeks=1))

    def _marketing():
        import models
        conn = models.get_conn()
        try:
            n = _posts(conn, rid, start, end)
            conn.commit()
        finally:
            conn.close()
        return n
    step("posts", _marketing)

    def _count():
        import random
        month_end = today.replace(day=1) - timedelta(days=1)
        rng = random.Random(f"hr-count|{month_end}")
        vals = {"beer": round(4600 * rng.uniform(0.88, 1.15), 2), "liquor": round(11800 * rng.uniform(0.92, 1.08), 2),
                "wine": round(3300 * rng.uniform(0.85, 1.15), 2)}
        return counts.save_count(rid, month_end.isoformat(), vals, note="Month-end count", source="import",
                                 today=today).get("saved")
    step("bar_count", _count)
    step("pruned", lambda: _prune(rid))

    def _learners():
        # The nightly feature pass reads trial and active accounts only; the showcase is internal
        # (so no sending job picks it up), so its own feature row, DNA and value point are written here.
        import models
        from intelligence import dna, features
        r = models.get_restaurant(rid)
        f = features.compute_and_store(rid, today=today + timedelta(days=1))
        dna.compute_and_store(rid, today=today + timedelta(days=1), features=f, restaurant=r, norms=dna.platform_norms())
        import value_delivered as vd
        vd.record_value_snapshot(rid, vd.compute_total_value_delivered(rid))
        return "ok"
    step("learners", _learners)
    out["failed"] = failed
    return out


def run_refresh() -> dict:
    """The demo_store_refresh job: every showcase restaurant (a demo-store token), one refresh each.
    Returns the standard counts."""
    import models
    conn = models.get_conn()
    try:
        demo_ids = [r["id"] for r in conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1").fetchall()]
    finally:
        conn.close()
    # The token is stored encrypted: read through get_restaurant, never matched in SQL.
    ids = [rid for rid in demo_ids if demo_store.is_simulated(rid)]
    ok = failed = 0
    for rid in ids:
        res = refresh(rid)
        if res.get("failed"):
            failed += 1
            log.warning("demo_store refresh %s: %s", rid, res)
        else:
            ok += 1
    return {"attempted": len(ids), "ok": ok, "failed": failed, "skipped": 0, "hit_bound": False}


ALL_STEPS = ("settings", "pos", "schedules", "dsr", "purchasing", "bar", "kitchen", "reviews", "neighborhood",
             "marketing", "people", "intel", "finish")


def run_all(steps=ALL_STEPS) -> int:
    """Create (or find) the showcase and run `steps` in order. Returns its restaurant id."""
    import time
    rid = ensure_account()
    _say(f"{demo_store.NAME} is restaurant {rid}; login {USERNAME!r}: {ensure_login(rid)}")
    for s in steps:
        t = time.time()
        if s == "pos":
            fill_pos(rid)
        else:
            globals()["fill_" + s](rid)
        _say(f"  [{s}] {time.time() - t:.0f}s")
    return rid
