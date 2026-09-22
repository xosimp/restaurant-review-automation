"""
demo_seed.py — the demo accounts (Gia Mia, Simple EJ's) and how they are
kept fresh.

This used to be ~1,000 lines inside models.py and ~460 inside the app
entry point. Nothing here runs for a real client: every function is gated
on the account's name or its `is_demo` flag, and the admin console's
"reseed demo data" refuses a restaurant that is not flagged.

Entry points:
  start_background_seed()   — hosted_dashboard calls this once at boot
  _refresh_gia_mia_reviews  — admin_routes' reseed route
  _seed_simple_ejs          — tests; also reachable as models._seed_simple_ejs
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

import config
from auth import create_user
import models as _models
from models import DB_PATH, Restaurant


# Resolved through models at call time, not bound at import: tests patch
# models.get_conn to point the seed at a fixture database (test_shift_quality
# does), and a bound copy here would have written to ./reviews.db instead —
# CLAUDE.md's bound-import hazard, met on the first day this module existed.
def get_conn(*a, **k): return _models.get_conn(*a, **k)
def create_restaurant(*a, **k): return _models.create_restaurant(*a, **k)
def get_capabilities(*a, **k): return _models.get_capabilities(*a, **k)
def get_restaurant(*a, **k): return _models.get_restaurant(*a, **k)
def save_client_data(*a, **k): return _models.save_client_data(*a, **k)
def set_capability(*a, **k): return _models.set_capability(*a, **k)
def update_restaurant(*a, **k): return _models.update_restaurant(*a, **k)


def _auto_seed_demo_clients():
    """Seed demo client data on every startup if not already seeded from a real upload."""
    try:
        _seed_gia_mia()
    except Exception as e:
        print(f"[auto-seed] Gia Mia seed failed: {e}")
    try:
        _seed_simple_ejs()
    except Exception as e:
        print(f"[auto-seed] Simple EJ's seed failed: {e}")


# ── Simple EJ's — the working demo account ────────────────────────────────
#
# A separate restaurant rather than a rename of the existing demo, because
# that one's hours notes, scheduling rules and role rates name Gia Mia and
# its wood-fired pizza inside the text that feeds the scheduling prompt, and
# its Place ID is Gia Mia's real Google listing. Renaming it would have put
# another restaurant's operating rules, reviews and competitors behind
# Erik's name.
#
# EVERY NUMBER BELOW IS A PLACEHOLDER. It is a plausible mid-size bar and
# grill, not Erik's real operation, and it is flagged as such in the
# restaurant's internal notes so nobody mistakes it for confirmed data.
# Replace it with his real CSV and hours the moment you have them.
SIMPLE_EJS_NAME = "Simple EJ's"
SIMPLE_EJS_EMAIL = "cavnarwill@gmail.com"

_EJS_ROSTER = [
    # name, role, operational score, typical start, typical end, hours
    ("Marcus R.", "Bartender", 5, "3:00pm", "11:30pm", 8.5),
    ("Devon K.",  "Bartender", 4, "4:00pm", "11:30pm", 7.5),
    ("Priya S.",  "Bartender", 3, "4:00pm", "10:30pm", 6.5),
    ("Cole T.",   "Bartender", 2, "5:00pm", "11:00pm", 6.0),
    ("Angela M.", "Server",    5, "10:30am", "5:00pm", 6.5),
    ("Reuben O.", "Server",    4, "4:00pm", "10:00pm", 6.0),
    ("Hana W.",   "Server",    3, "11:00am", "5:00pm", 6.0),
    ("Trey B.",   "Server",    3, "4:30pm", "10:30pm", 6.0),
    ("Simone A.", "Server",    2, "5:00pm", "10:00pm", 5.0),
    ("Vince L.",  "Line Cook", 5, "2:00pm", "11:00pm", 9.0),
    ("Omar H.",   "Line Cook", 4, "3:00pm", "11:00pm", 8.0),
    ("Bea C.",    "Line Cook", 3, "3:00pm", "10:30pm", 7.5),
    ("Nico F.",   "Line Cook", 1, "4:00pm", "10:00pm", 6.0),
    ("Jules P.",  "Prep Cook", 4, "8:00am", "3:30pm", 7.5),
    ("Ari D.",    "Prep Cook", 3, "8:00am", "3:00pm", 7.0),
    ("Tessa G.",  "Host",      4, "4:00pm", "10:00pm", 6.0),
    ("Milo J.",   "Host",      3, "11:00am", "4:30pm", 5.5),
    ("Kase N.",   "Busser",    3, "4:30pm", "10:30pm", 6.0),
    ("Lupe V.",   "Busser",    2, "4:30pm", "10:30pm", 6.0),
]

# Who is trusted to lock up. Deliberately not the highest scores: being
# trusted with keys and cash is a different thing from being good on a
# Saturday, which is the whole reason the capability layer stores it as a
# flag rather than a point on the rating scale.
_EJS_CLOSERS = ("Marcus R.", "Angela M.", "Vince L.")

_EJS_HOURS = (
    "RESTAURANT HOURS: Open 11:00am Mon-Sat, 10:00am Sunday. "
    "Close: 10:00pm Sun-Wed; 12:00am Thu-Sat.\n\n"
    "STAFF ARRIVAL TIMES:\n"
    "- Prep cooks: arrive 8:00am every day.\n"
    "- Line cooks: first arrives 2:00pm; others stagger from 3:00pm.\n"
    "- Servers: first arrives 10:30am for side work before open.\n"
    "- Bartenders: evening only, no earlier than 3:00pm. Stay 30 minutes "
    "after close to break down the bar.\n"
    "- Hosts: one on at open, a second from 4:00pm Thu-Sat.\n\n"
    "SHIFT END / CLOSER RULES:\n"
    "- Keep 2 servers through close; cut the rest about an hour after the "
    "dinner rush drops.\n"
    "- 1 line cook always stays through close.\n"
    "- Bussers cut 30 minutes before close.\n"
    "- Thu-Sat: 2 bartenders close together; Sun-Wed one is enough.\n\n"
    "MINIMUM STAFFING FLOORS:\n"
    "- Servers: minimum 2 on the floor during any open hour; maximum 5 at once.\n"
    "- Line cooks: minimum 2 for dinner service every night, 3 Thu-Sat.\n"
    "- Bartenders: minimum 1 whenever the bar is open, 2 from 5:00pm Thu-Sat.\n"
    "- Hosts: minimum 1 whenever the dining room is open.\n"
    "- Bussers: minimum 1 at night, 2 Fri and Sat.\n\n"
    "SHIFT LENGTHS:\n"
    "- Servers 5-7h, bartenders 6-9h, line cooks 7-9h, prep 7-8h, "
    "hosts 5-6h, bussers 5-6h."
)

_EJS_SCHED_NOTES = (
    "Thursday through Saturday nights are the week. Sunday is a steady "
    "all-day trade rather than a rush. Monday and Tuesday are the quiet "
    "pair and are where somebody new should be learning."
)

_EJS_ROLE_RATES = {
    "Bartender": 9.00, "Server": 9.00, "Busser": 9.00, "Host": 15.00,
    "Line Cook": 21.00, "Prep Cook": 19.00,
}

_EJS_CLOSE_TIMES = {
    "Sunday": "10:00pm", "Monday": "10:00pm", "Tuesday": "10:00pm",
    "Wednesday": "10:00pm", "Thursday": "12:00am", "Friday": "12:00am",
    "Saturday": "12:00am",
}

# Bartenders are the one role authorised past close, to break down the bar.
_EJS_CLOSE_BUFFER = {"Bartender": 30}

# Per-role combined Operational Score targets, and the one leadership rule
# Erik described: a strong bartender on the busiest night.
_EJS_STRENGTH = {"Bartender": 8, "Line Cook": 9, "Server": 9}
_EJS_LEADER_RULES = [
    {"role": "Bartender", "days": ["Friday", "Saturday"], "daypart": "night",
     "min_score": 5, "count": 1},
    {"closing": True, "role": "Bartender", "attribute": "can_close", "count": 1},
]


def _seed_simple_ejs(db_path: str = DB_PATH):
    """Create and seed the Simple EJ's demo account, idempotently.

    Never touches a restaurant carrying real uploaded shifts, never touches
    Gia Mia, and never writes settings over an admin edit — the same three
    guards the existing demo seed uses, for the same reason: a redeploy must
    not silently undo work done in the admin panel.
    """
    from datetime import date, timedelta
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, is_demo FROM restaurants WHERE name=? LIMIT 1",
                           (SIMPLE_EJS_NAME,)).fetchone()
    finally:
        conn.close()

    if row:
        rid = row["id"]
        if not row["is_demo"]:
            print(f"[auto-seed] {SIMPLE_EJS_NAME} (id={rid}) is no longer flagged demo — leaving it alone")
            return rid
    else:
        rid = create_restaurant(Restaurant(
            name=SIMPLE_EJS_NAME, owner_email=SIMPLE_EJS_EMAIL, owner_name="Erik",
            is_demo=1, module_reviews=1, module_labor=1, module_inventory=1,
            module_marketing=1, service_tier="full", timezone="America/Chicago",
            hourly_rate=12.50, labor_target_pct=26.0,
        ), db_path=db_path)
        print(f"[auto-seed] created {SIMPLE_EJS_NAME} as id={rid}")

    _seed_ejs_history(rid, db_path)
    _seed_ejs_settings(rid, db_path)
    _seed_ejs_shifts(rid, db_path)
    _seed_ejs_capabilities(rid, db_path)
    _seed_ejs_food_cost(rid, db_path)
    _ensure_ejs_login(rid, db_path)
    return rid


def _seed_ejs_history(rid: int, db_path: str):
    """Four weeks of daily sales and hours, so demand, year-over-year and the
    PAR budget all have something real to work from."""
    from datetime import date, timedelta
    # Monday quiet through Saturday peak. Sunday is steady all-day trade.
    BY_WEEKDAY = {0: (4100, 52), 1: (3900, 50), 2: (5200, 61), 3: (7400, 78),
                  4: (11800, 116), 5: (13200, 128), 6: (8600, 90)}
    conn = get_conn(db_path)
    try:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        d = date(2025, 6, 2)
        while d <= date(2025, 6, 29):
            sales, hours = BY_WEEKDAY[d.weekday()]
            cost = round(hours * 12.5, 2)
            conn.execute("""INSERT OR REPLACE INTO labor_daily_history
                (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales,
                 total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (rid, d.strftime("%Y-%m-%d"), days[d.weekday()],
                 round(cost / sales * 100, 2), cost, float(sales), float(hours)))
            d += timedelta(days=1)
        # And the trailing six weeks, anchored to today: food cost % divides
        # purchases by the sales of the same window, and a window that ends
        # today needs sales through today. OR IGNORE — a row a POS sync
        # wrote for a real date is never overwritten by a placeholder.
        d = date.today() - timedelta(days=41)
        while d <= date.today():
            sales, hours = BY_WEEKDAY[d.weekday()]
            cost = round(hours * 12.5, 2)
            conn.execute("""INSERT OR IGNORE INTO labor_daily_history
                (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales,
                 total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (rid, d.strftime("%Y-%m-%d"), days[d.weekday()],
                 round(cost / sales * 100, 2), cost, float(sales), float(hours)))
            d += timedelta(days=1)
        conn.commit()
    finally:
        conn.close()


def _seed_ejs_settings(rid: int, db_path: str):
    """Hours, rates, close times and targets — only on a restaurant that has
    never been configured, so an admin edit is never overwritten."""
    existing = get_restaurant(rid, db_path)
    if existing and (existing.hours_notes or "").strip():
        return
    update_restaurant(rid, {
        "monthly_revenue_target": 232000.0,
        "labor_target_pct": 26.0,
        "hourly_rate": 12.50,
        "hours_notes": _EJS_HOURS,
        "sched_notes": _EJS_SCHED_NOTES,
        "section_count": 5,
        "daypart_split": "lunch 30%, dinner 70%",
        "role_rates_json": json.dumps(_EJS_ROLE_RATES),
        "close_times_json": json.dumps(_EJS_CLOSE_TIMES),
        "role_close_buffer_json": json.dumps(_EJS_CLOSE_BUFFER),
        "role_strength_json": json.dumps(_EJS_STRENGTH),
        "shift_leader_rules_json": json.dumps(_EJS_LEADER_RULES),
        "role_minimums_json": json.dumps({"Bartender": 1, "Server": 2, "Line Cook": 2}),
        "internal_notes": ("DEMO ACCOUNT. Every figure here is a placeholder written to "
                           "give the product something realistic to run on — hours, wages, "
                           "sales, roster and ratings are all invented. Replace with Erik's "
                           "real CSV and hours before treating any number as his."),
        "email_theme": "dark",
    })
    print(f"[auto-seed] {SIMPLE_EJS_NAME} settings written")


def _seed_ejs_shifts(rid: int, db_path: str):
    """Two weeks of shifts, generated rather than hand-written so the roster,
    the day-of-week volume and the role mix stay consistent with each other."""
    from datetime import date, timedelta
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT shifts_source FROM client_data WHERE restaurant_id=?",
                           (rid,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if row and row["shifts_source"] in ("upload", "toast"):
        return   # a real upload always wins

    BY_WEEKDAY = {0: (4100, 0.55), 1: (3900, 0.55), 2: (5200, 0.7), 3: (7400, 0.85),
                  4: (11800, 1.0), 5: (13200, 1.0), 6: (8600, 0.8)}
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,"
             "actual_hours,sales,notes"]
    d = date(2026, 8, 31)                      # a Monday
    for _ in range(14):
        sales, share = BY_WEEKDAY[d.weekday()]
        # A quieter day drops the back half of each role rather than
        # thinning every role evenly — which is how a real rota shrinks.
        by_role = {}
        for name, role, _score, start, end, hours in _EJS_ROSTER:
            by_role.setdefault(role, []).append((name, start, end, hours))
        for role, people in by_role.items():
            keep = max(1, int(round(len(people) * share)))
            for name, start, end, hours in people[:keep]:
                lines.append(f"{d.strftime('%Y-%m-%d')},{days[d.weekday()]},{name},{role},"
                             f"{start},{end},{hours},{hours},{sales},")
        d += timedelta(days=1)
    save_client_data(rid, "shifts", "\n".join(lines), source="seed")
    print(f"[auto-seed] {SIMPLE_EJS_NAME} shift data written ({len(lines) - 1} rows)")


def _seed_ejs_capabilities(rid: int, db_path: str):
    """Operational Scores and closer flags, so the Shift Quality engine has
    something to show rather than sitting dormant behind a demo."""
    existing = get_capabilities(rid, db_path=db_path)
    if existing:
        return   # already rated, by this seed or by hand
    for name, _role, score, _s, _e, _h in _EJS_ROSTER:
        try:
            set_capability(rid, name, score=score, updated_by="seed", db_path=db_path)
        except Exception:
            pass
    for name in _EJS_CLOSERS:
        try:
            set_capability(rid, name, attribute="can_close", flag=True,
                           updated_by="seed", db_path=db_path)
        except Exception:
            pass
    print(f"[auto-seed] {SIMPLE_EJS_NAME} ratings written for {len(_EJS_ROSTER)} staff")


# ── Food Cost: a pantry with suppliers, a priced menu with recipes, six
#    weekly counts and a delivery a week ─────────────────────────────────────
#
# Without these the Food Cost tab ran on the sample pantry: no actual food
# cost % (no counts, no purchases, no sales in the window), no margins (no
# dish had a recipe or a price), nothing to order (no supplier on any
# ingredient). Quantities are sized to a ~$54k/week restaurant at roughly
# 27% food cost and ~4.5% waste, so every figure lands near its target
# rather than screaming. Supplier addresses are .example — undeliverable
# by definition, so "Send" on the demo never reaches a real vendor.

_EJS_SUPPLIERS = {
    "broadline": ("Sysco Chicago", "orders@sysco-demo.example"),
    "produce": ("Testa Produce", "orders@testa-demo.example"),
}

# name, category, unit, unit cost, weekly delivery qty, par, on hand, waste last week, supplier
_EJS_PANTRY = [
    ("Chicken Breast", "Protein", "lb", 5.80, 240, 80, 44, 6.0, "broadline"),
    ("Ground Beef 80/20", "Protein", "lb", 6.20, 300, 100, 36, 8.0, "broadline"),
    ("Ribeye Steak", "Protein", "lb", 19.50, 180, 60, 18, 2.4, "broadline"),
    ("Salmon Fillet", "Protein", "lb", 16.50, 120, 40, 12, 3.6, "broadline"),
    ("Shrimp 16/20", "Protein", "lb", 14.20, 100, 36, 10, 3.0, "broadline"),
    ("Cheddar Cheese", "Dairy", "lb", 5.40, 90, 30, 24, 1.6, "broadline"),
    ("Butter", "Dairy", "lb", 4.50, 80, 28, 32, 1.0, "broadline"),
    ("Heavy Cream", "Dairy", "qt", 3.80, 60, 20, 14, 2.0, "broadline"),
    ("Burger Buns", "Bakery", "each", 0.55, 1300, 440, 280, 80, "broadline"),
    ("Fries (frozen)", "Pantry", "lb", 1.60, 520, 180, 140, 12, "broadline"),
    ("Penne Pasta", "Pantry", "lb", 2.80, 100, 36, 40, 1.2, "broadline"),
    ("Olive Oil", "Pantry", "bottle", 14.50, 20, 8, 10, 0.0, "broadline"),
    ("Romaine Lettuce", "Produce", "head", 2.50, 240, 80, 52, 28, "produce"),
    ("Roma Tomatoes", "Produce", "lb", 1.80, 180, 60, 40, 18, "produce"),
    ("Yellow Onions", "Produce", "lb", 0.95, 160, 56, 48, 8, "produce"),
    ("Russet Potatoes", "Produce", "lb", 0.80, 280, 100, 90, 14, "produce"),
    ("Fresh Herbs", "Produce", "bunch", 5.50, 36, 12, 8, 5.0, "produce"),
]

# dish, sell price, (ingredient, qty per plate)
_EJS_MENU = [
    ("Classic Burger", 14.00, [("Ground Beef 80/20", 0.4), ("Burger Buns", 1), ("Cheddar Cheese", 0.1),
                               ("Romaine Lettuce", 0.1), ("Roma Tomatoes", 0.15), ("Fries (frozen)", 0.35)]),
    ("Grilled Chicken Sandwich", 13.00, [("Chicken Breast", 0.45), ("Burger Buns", 1), ("Romaine Lettuce", 0.1),
                                         ("Roma Tomatoes", 0.12), ("Fries (frozen)", 0.35)]),
    ("Ribeye & Potatoes", 36.00, [("Ribeye Steak", 0.75), ("Russet Potatoes", 0.6), ("Butter", 0.08), ("Fresh Herbs", 0.1)]),
    ("Grilled Salmon", 24.00, [("Salmon Fillet", 0.5), ("Russet Potatoes", 0.4), ("Butter", 0.06), ("Olive Oil", 0.02)]),
    ("Shrimp Tacos", 16.00, [("Shrimp 16/20", 0.35), ("Romaine Lettuce", 0.15), ("Roma Tomatoes", 0.12), ("Yellow Onions", 0.1)]),
    ("Chicken Alfredo", 17.00, [("Chicken Breast", 0.4), ("Penne Pasta", 0.3), ("Heavy Cream", 0.25), ("Butter", 0.05)]),
    ("Caesar Salad", 11.00, [("Romaine Lettuce", 0.6), ("Olive Oil", 0.03), ("Cheddar Cheese", 0.05)]),
    ("Loaded Fries", 9.00, [("Fries (frozen)", 0.6), ("Cheddar Cheese", 0.15), ("Yellow Onions", 0.08)]),
]


def _seed_ejs_food_cost(rid: int, db_path: str):
    """Only on a restaurant with no ingredient of its own and no uploaded or
    POS-fed inventory; a second boot finds the ingredients and returns."""
    import math
    from datetime import date, timedelta
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM ingredients WHERE restaurant_id=? LIMIT 1", (rid,)).fetchone():
            return
        try:
            src = conn.execute("SELECT inventory_source FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
        except Exception:
            src = None
        if src and src["inventory_source"] in ("upload", "toast"):
            return
        today = date.today()
        iso = today.isoformat()

        ids = {}
        for name, cat, unit, cost, weekly, par, stock, waste, sup in _EJS_PANTRY:
            sname, semail = _EJS_SUPPLIERS[sup]
            cur = conn.execute(
                "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, case_size, "
                " current_stock, avg_daily_usage, last_order_qty, waste_last_week, last_recount_at, "
                " supplier_name, supplier_email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, name, cat, unit, par, cost, 1.0, stock, round(weekly / 7, 2), weekly, waste, iso, sname, semail))
            iid = cur.lastrowid
            ids[name] = iid
            # A delivery a week for five weeks (the four inside a 28-day window
            # are what food cost % divides by), then the count that anchors the
            # ledger — last, so it is the newest recount and the stock on hand
            # is exactly the counted figure.
            for k in range(5, 0, -1):
                conn.execute(
                    "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                    " source, note) VALUES (?,?,?,?,?,?,?)",
                    (rid, iid, "receiving", weekly, (today - timedelta(days=7 * k - 2)).isoformat(), "seed", "weekly delivery"))
            conn.execute(
                "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                " source, note) VALUES (?,?,?,?,?,?,?)",
                (rid, iid, "recount", stock, iso, "seed", "count that anchors the ledger"))

        for dish, price, lines in _EJS_MENU:
            row = conn.execute("SELECT id FROM menu_items WHERE restaurant_id=? AND name=? AND is_active=1",
                               (rid, dish)).fetchone()
            mid = row["id"] if row else conn.execute(
                "INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,NULL,?,?)",
                (rid, dish, price)).lastrowid
            if row:
                conn.execute("UPDATE menu_items SET sell_price=COALESCE(sell_price, ?) WHERE id=?", (price, mid))
            for ing, qty in lines:
                if not conn.execute("SELECT 1 FROM recipe_ingredients WHERE menu_item_id=? AND ingredient_id=?",
                                    (mid, ids[ing])).fetchone():
                    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                                 (mid, ids[ing], qty))

        # Six weekly counts with per-item detail. The sample-era snapshots
        # (no items, written while the page showed example data) go — they
        # would sit beside real weeks in the chart as $0-value counts.
        conn.execute("DELETE FROM inventory_history WHERE restaurant_id=? AND items_json IS NULL", (rid,))
        for k in range(5, -1, -1):
            week_end = (today - timedelta(days=7 * k)).isoformat()
            items, inv_value, waste_cost, top = [], 0.0, 0.0, []
            for i, (name, cat, unit, cost, weekly, par, stock, waste, sup) in enumerate(_EJS_PANTRY):
                st = round(stock * (1 + 0.18 * math.sin(k * 1.3 + i)), 1)
                ws = round(waste * (1 + 0.35 * math.cos(k * 1.7 + i * 0.6)), 1)
                items.append({"item": name, "category": cat, "unit": unit, "unit_cost": cost, "par_level": par,
                              "current_stock": st, "avg_daily_usage": round(weekly / 7, 2),
                              "last_order_qty": weekly, "waste_last_week": ws})
                inv_value += st * cost
                waste_cost += ws * cost
                top.append((ws * cost, name))
            top.sort(reverse=True)
            snap = {"total_waste_cost": round(waste_cost, 2), "top_items": [n for _, n in top[:4]],
                    "inventory_value": round(inv_value, 2)}
            ex = conn.execute("SELECT id FROM inventory_history WHERE restaurant_id=? AND week_end=?",
                              (rid, week_end)).fetchone()
            if ex:
                conn.execute("UPDATE inventory_history SET waste_json=?, items_json=?, inv_value=?, source='seed' WHERE id=?",
                             (json.dumps(snap), json.dumps(items), round(inv_value, 2), ex["id"]))
            else:
                conn.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, source) "
                             "VALUES (?,?,?,?,?,?)",
                             (rid, json.dumps(snap), week_end, json.dumps(items), round(inv_value, 2), "seed"))
        conn.commit()
    finally:
        conn.close()
    print(f"[auto-seed] {SIMPLE_EJS_NAME} food cost seeded: {len(_EJS_PANTRY)} ingredients, {len(_EJS_MENU)} dishes")


SIMPLE_EJS_USERNAME = "erik"


def _ensure_ejs_login(rid: int, db_path: str):
    """A login for the demo account.

    Set DEMO_PASSWORD and the same credential works in every environment,
    which is the point: a password generated per environment meant the one
    on your laptop was not the one on Railway, and finding the live one
    meant digging through the admin client card first.

    Without that variable it falls back to a random password, printed once
    and stored in restaurants.temp_password where the admin client card
    already shows it — the same place the Add Client form puts a new
    client's first password.
    """
    import os as _os
    import secrets
    from auth import create_user
    from werkzeug.security import generate_password_hash

    wanted = (_os.getenv("DEMO_PASSWORD") or "").strip()
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, username FROM users WHERE restaurant_id=? LIMIT 1", (rid,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()

    if row:
        # Re-point an existing demo login at DEMO_PASSWORD, because the
        # account is usually created before the variable is set and a
        # password nobody can reproduce is no use. Guarded three ways: the
        # variable has to be set, the restaurant has to still be flagged as
        # a demo, and it has to be the login this seed made. A real client's
        # password is never touched.
        restaurant = get_restaurant(rid, db_path)
        if not (wanted and restaurant and restaurant.is_demo
                and row["username"] == SIMPLE_EJS_USERNAME):
            return
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (generate_password_hash(wanted), row["id"]))
            conn.commit()
        finally:
            conn.close()
        update_restaurant(rid, {"temp_password": wanted})
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login reset to DEMO_PASSWORD "
              f"(username {SIMPLE_EJS_USERNAME!r})")
        return

    password = wanted or secrets.token_urlsafe(9)
    try:
        create_user(rid, SIMPLE_EJS_USERNAME, "erik+demo@cavnar.ai", password, db_path=db_path)
        update_restaurant(rid, {"temp_password": password})
        source = "DEMO_PASSWORD" if wanted else f"generated: {password}"
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login created — username "
              f"{SIMPLE_EJS_USERNAME!r}, password from {source} "
              "(also on the admin client card)")
    except Exception as e:
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login not created: {e}")


def _seed_gia_mia(db_path: str = DB_PATH):
    """Seed Gia Mia (id=2) labor history + shift CSV unless real upload exists."""
    from datetime import date, timedelta
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Check if real (non-seed) shifts already uploaded
    try:
        row = conn.execute(
            "SELECT shifts_source FROM client_data WHERE restaurant_id=2"
        ).fetchone()
    except Exception:
        conn.close()
        return
    skip_shift_seed = row and row["shifts_source"] in ("upload", "toast")
    conn.close()

    if not skip_shift_seed:
        # Seed June 2025 daily history (YoY context for schedule generation)
        _conn2 = sqlite3.connect(db_path, timeout=5)
        _conn2.row_factory = sqlite3.Row
        _conn2.execute("PRAGMA journal_mode=WAL")
        DAY_TEMPLATES = {
            0: {"sales": 8200,  "hours": 71},
            1: {"sales": 8800,  "hours": 76},
            2: {"sales": 10500, "hours": 91},
            3: {"sales": 12200, "hours": 105},
            4: {"sales": 16400, "hours": 142},
            5: {"sales": 18800, "hours": 163},
            6: {"sales": 13200, "hours": 114},
        }
        HOLIDAY_OVERRIDES = {"2025-06-15": {"sales": 22400, "hours": 194}}
        DAYS_OF_WEEK = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        d = date(2025, 6, 2)
        while d <= date(2025, 6, 29):
            ds = d.strftime("%Y-%m-%d")
            tmpl = HOLIDAY_OVERRIDES.get(ds, DAY_TEMPLATES[d.weekday()])
            sales = float(tmpl["sales"])
            hours = float(tmpl["hours"])
            labor_cost = round(hours * 26, 2)
            labor_pct  = round(labor_cost / sales * 100, 2)
            _conn2.execute("""
                INSERT OR REPLACE INTO labor_daily_history
                  (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
            """, (2, ds, DAYS_OF_WEEK[d.weekday()], labor_pct, labor_cost, sales, hours))
            d += timedelta(days=1)
        _conn2.commit()
        _conn2.close()

    gia_mia_hours = (
        "RESTAURANT HOURS: Open 11:00am daily. "
        "Close: 9:00pm Sun–Wed; 10:00pm Thu–Sat.\n\n"
        "STAFF ARRIVAL TIMES (hard rules — do not deviate):\n"
        "- Bussers: arrive 8:00am every day.\n"
        "- Cooks: arrive 8:30am Mon–Thu; arrive 8:00am Fri, Sat, Sun.\n"
        "- Servers: arrive 10:00am every day (1 hour before 11am open for side work).\n"
        "- Bartenders: NO morning shifts — evening only. Start no earlier than 3:00pm. "
        "Stay 1 hour after close: until 10:00pm Sun–Wed; until 11:00pm Thu–Sat.\n"
        "- Food Runners: arrive with kitchen for dinner service. "
        "On Pizza Mondays and Fridays, also scheduled for lunch.\n\n"
        "SHIFT END / CLOSER RULES:\n"
        "- Always keep 2 servers as closers (until restaurant close time).\n"
        "- Cut all other servers 1–1.5h before close when volume allows.\n"
        "- Fri/Sat: 3 server closers + 2 bartender closers.\n"
        "- Cooks: 1 cook always stays through close; cut others 45min–1h early on slow nights.\n"
        "- Bussers cut 30min before close.\n"
        "- Food Runners: cut after dinner rush, typically 1–2h before close.\n\n"
        "FLOOR LAYOUT & SECTIONS:\n"
        "- 31 tables inside, 24 tables on patio (patio open May–Labor Day).\n"
        "- Sections: 10s, 20s, 30s, 40s, PDR (private dining room — events only), outside patio.\n"
        "- Each server handles approximately 6 tables per section.\n"
        "- HARD CAP: never schedule more than 7 servers at once. "
        "Only in extreme circumstances would 8 ever be needed — avoid this.\n\n"
        "SERVER STAGGER RULES:\n"
        "- Exactly ONE server opens (arrives 10:00am, 1h before 11am open).\n"
        "- Second server starts at 11am (open) or 11:30am depending on day volume.\n"
        "- Additional servers only at 12pm+ and only when YoY/event data backs it up.\n"
        "- Never two servers at the same start time.\n\n"
        "MINIMUM STAFFING FLOORS:\n"
        "- Servers: minimum 3 on the floor during ANY open service hour, every single day, "
        "morning and night alike -- 2 is never enough to cover the floor, that's a hard "
        "rule regardless of how slow TYPICAL HEADCOUNT makes a given shift look. Maximum 7 "
        "at once.\n"
        "- Double shifts: before adding closers for dinner/night service, first count "
        "anyone ALREADY on the floor from a double shift that day (scheduled for both "
        "morning and night) -- they already count toward the night total, don't add new "
        "closers on top of them as if the floor were starting from zero. Add up double-"
        "shift carryovers plus newly-scheduled closers and check that total against both "
        "the 3-minimum floor above and the 7-person hard cap before finalizing -- 8 "
        "servers at once has never actually happened at Gia Mia and should never be "
        "scheduled.\n"
        "- Cooks/Kitchen for morning/lunch service: minimum 2 people total, every single "
        "day -- 1 Prep/Pantry cook alone is never enough. Scale to 3 on Pizza Monday and "
        "weekends. This is a hard floor, not a historical average, same as the dinner "
        "floor below -- a morning with only 1 cook and 1 server on is never correct, no "
        "matter what day it is.\n"
        "- Cooks/Kitchen for dinner/night service: minimum 4 people total, every single "
        "night. Count them: 1 on Pizza (wood-fired pizza is Gia Mia's signature dish, so "
        "Pizza station is never left empty during service, no exceptions) + 3 on "
        "Pantry/Saute = 4 minimum. On Pizza Monday and weekends, scale to 2 on Pizza + 5 "
        "on Pantry/Saute = 7 minimum. This is a hard floor, not a historical average -- "
        "even on the slowest night of the week, count your kitchen rows and confirm there "
        "are at least 4 before finalizing, regardless of what TYPICAL HEADCOUNT shows.\n"
        "- Pizza Cook: minimum 1 on the Pizza station at all times, every open service "
        "hour, all 7 days -- morning/lunch AND dinner/night alike, no exceptions. The "
        "wood-fired oven is never left unstaffed, regardless of what TYPICAL HEADCOUNT "
        "shows for that specific shift. This is one of the people already counted in the "
        "Cooks/Kitchen totals above (already explicit for dinner: 1 of the 4; for "
        "morning/lunch, 1 of the 2) -- not an extra body added on top, but that slot must "
        "specifically be a Pizza Cook, not just any cook.\n"
        "- Bartenders: minimum 1 whenever the bar is open.\n"
        "- Hosts: minimum 1 whenever the dining room is open.\n"
        "- Bussers, NIGHT service: minimum 2 on at once, every single night, no exceptions "
        "-- this applies all 7 days, not just Pizza Monday/weekends. This is a hard floor, "
        "not a historical average.\n"
        "- Bussers, MORNING/lunch service: minimum 2 on at once every day EXCEPT Tuesday, "
        "Wednesday, and Thursday, where 1 is acceptable if that's what TYPICAL HEADCOUNT "
        "shows. Monday, Friday, Saturday, Sunday mornings still need 2 minimum.\n"
        "- Food Runners: see FOOD RUNNER RULES below for the full pattern.\n\n"
        "SHIFT LENGTHS:\n"
        "- Servers: 4–7h. Openers run 6–7h through lunch. Closers run 5–7h.\n"
        "- Bartenders: 6–9h. Closers stay 1h after restaurant close.\n"
        "- Cooks: 6–10h. Kitchen closers often need 9–10h for full service + breakdown.\n"
        "- Hosts: 5–8h. One opener, close when last table is seated.\n"
        "- Food Runners: 4–6h dinner-only; 8–10h on days they run both lunch and dinner.\n"
        "- Bussers: 6–9h.\n\n"
        "PIZZA MONDAY RULE:\n"
        "- Monday is Pizza Monday (half-price pizzas all day) — significantly busier than a typical Monday.\n"
        "- Staff Monday closer to a busy Friday than a slow weekday (see FOOD RUNNER RULES below for its food runner coverage).\n\n"
        "FOOD RUNNER RULES:\n"
        "- 1 food runner on for dinner/night service every night of the week — baseline, no exceptions.\n"
        "- Pizza Monday and weekend nights (Fri, Sat, Sun): 2 food runners for dinner/night service.\n"
        "- Occasionally — only if labor % and volume genuinely support it — add 1 food runner for a weekday "
        "morning/lunch shift (Tue-Thu). This is optional and should be rare; never schedule it as a fixed weekly requirement.\n"
        "- Never more than 2 food runners at once, except for a private PDR event."
    )
    gia_mia_sched_notes = (
        "Monday is Pizza Monday — treat Monday lunch like a busy Friday for staffing (see hours notes for food "
        "runner coverage specifics). "
        "Hard cap: never exceed 7 servers on floor at once. "
        "PDR (private dining room) is separate from floor sections and requires a dedicated server for private events."
    )
    # Real base wages (Illinois — a tip-credit state, so tipped roles carry
    # a low base wage; tips are guest money, not a restaurant labor cost, so
    # base wage is the right figure for labor-cost-% and PAR math, not a
    # loaded base+tip-makeup number). Confirmed with the client. Carry Out
    # is host duty (see gia_mia_sched_notes), so it shares the Host rate.
    # Runner and Shift Supervisor rates are this session's own estimate
    # (tipped-support parity with Busser/Bartender for Runner; a modest
    # premium over Server base for the added Shift Supervisor
    # responsibility) — not confirmed with the client, flagged here so
    # they're easy to find and correct later.
    gia_mia_role_rates = {
        "Server": 9.00, "Host": 15.00, "Busser": 9.00, "Bartender": 9.00,
        "Pantry Cook": 22.00, "Prep Cook": 22.00, "Saute Cook": 22.00, "Pizza Cook": 22.00,
        "Runner": 9.00, "Carry Out": 15.00, "Shift Supervisor": 12.00,
    }
    # Matches hours_notes' own "RESTAURANT HOURS" line exactly — the
    # generator was told this in prose already, but prose alone let it
    # occasionally borrow Thu-Sat's later close for a Sun-Wed night (e.g.
    # scheduling Monday servers to 9:30-10pm when Monday actually closes at
    # 9pm). This structured copy is what the post-generation enforcement in
    # client_api.py checks every shift_end against — a hard cap, not a
    # request.
    gia_mia_close_times = {
        "Sunday": "9:00pm", "Monday": "9:00pm", "Tuesday": "9:00pm", "Wednesday": "9:00pm",
        "Thursday": "10:00pm", "Friday": "10:00pm", "Saturday": "10:00pm",
    }
    # The only role hours_notes explicitly authorizes to run past close
    # ("Stay 1 hour after close"). Every other role defaults to 0 — must
    # end at or before that day's close time.
    gia_mia_role_close_buffer = {"Bartender": 60}
    # Only apply on a genuinely fresh/never-configured restaurant (or one
    # that somehow lost its notes) — this used to run unconditionally on
    # every single server restart, which meant any admin-panel edit to
    # these exact fields (hours_notes, role_rates_json, close_times_json,
    # etc. — all editable from client_settings.html) got silently
    # overwritten back to these hardcoded values the next time the server
    # redeployed. hours_notes empty is the signal "still needs seeding";
    # once it's set (by this block or by a real admin edit), it never gets
    # blown away again just because the process restarted.
    _existing = get_restaurant(2, db_path)
    if not _existing or not (_existing.hours_notes or "").strip():
        # Use update_restaurant so the correct DB connection path is always used
        update_restaurant(2, {
            "monthly_revenue_target": 365000.0,
            "labor_target_pct": 23.0,
            # Was a flat 26.0 — nowhere close to any real role's actual wage,
            # which meant PAR's hours_budget (dollars / rate) was computed
            # against a rate roughly double the real weighted blended rate
            # (~$13.53/hr given the current role/hours mix), understating
            # achievable hours by about half. role_rates_json below is the
            # real fix (per-role, used everywhere cost is computed); this flat
            # value now only matters as the last-resort fallback for a role
            # that isn't in role_rates_json, so it's set to roughly match the
            # real blended rate rather than being wildly high.
            "hourly_rate": 13.50,
            "hours_notes": gia_mia_hours,
            "sched_notes": gia_mia_sched_notes,
            "section_count": 7,
            "daypart_split": "lunch 40%, dinner 60%",
            "role_rates_json": json.dumps(gia_mia_role_rates),
            "close_times_json": json.dumps(gia_mia_close_times),
            "role_close_buffer_json": json.dumps(gia_mia_role_close_buffer),
            "location_name": "St. Charles, IL",
            "email_theme": "dark",
        })
        print("[auto-seed] Restaurant settings updated: labor_target=23%, monthly_revenue=$365k")
    else:
        print("[auto-seed] Restaurant settings already configured — skipping (won't clobber admin edits)")

    gia_mia_csv = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-01,Monday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Rosa M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-01,Monday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-01,Monday,Cody M.,Busser,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-01,Monday,Rhea D.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,James H.,Host,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-01,Monday,Piper A.,Host,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Freya S.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Ivy R.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-01,Monday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-01,Monday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-01,Monday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-01,Monday,Felix G.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-01,Monday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-01,Monday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Bella C.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Derek M.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Jamie L.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Mason C.,Server,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Maya R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Noah K.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Priya K.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Rosa M.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Sienna P.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Zoe H.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-02,Tuesday,James H.,Carry Out,4:00pm,10:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Marisol T.,Host,11:00am,5:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Carlos B.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Hector M.,Pantry Cook,10:30am,6:30pm,8.0,8.0,9000,
2026-06-02,Tuesday,Wei C.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Miles B.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Nikolai P.,Prep Cook,8:00am,3:30pm,7.5,7.5,9000,
2026-06-02,Tuesday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,9000,
2026-06-02,Tuesday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,9000,
2026-06-02,Tuesday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Priya K.,Server,5:00pm,11:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-03,Wednesday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,James H.,Host,11:00am,5:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Owen K.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Dante F.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Hector M.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-03,Wednesday,Leo K.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-03,Wednesday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Camila G.,Prep Cook,8:00am,3:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Duncan L.,Prep Cook,8:00am,3:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,11000,
2026-06-03,Wednesday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Jamie L.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Kim T.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Liam P.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Priya K.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Xavier R.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Zoe H.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-04,Thursday,Derek M.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Rosa M.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Delphine A.,Busser,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Xavier R.,Carry Out,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,James H.,Host,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Rhea D.,Host,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Simone R.,Pantry Cook,8:00am,4:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Jasper N.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,13000,
2026-06-04,Thursday,Kenji H.,Prep Cook,10:00am,5:30pm,7.5,7.5,13000,
2026-06-04,Thursday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,13000,
2026-06-04,Thursday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Isla V.,Saute Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Layla N.,Saute Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Nora J.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Ava S.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Grace T.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Omar T.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Ruby F.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Sofia R.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-05,Friday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-05,Friday,Tomas H.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-05,Friday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-05,Friday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Bella C.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-05,Friday,Lena S.,Carry Out,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Sienna P.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Hector M.,Pantry Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Simone R.,Pantry Cook,10:30am,6:30pm,8.0,8.0,18000,
2026-06-05,Friday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,18000,
2026-06-05,Friday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-05,Friday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-05,Friday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,18000,
2026-06-05,Friday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-05,Friday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-05,Friday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,18000,
2026-06-05,Friday,Diego L.,Server,4:00pm,9:30pm,5.5,5.5,18000,
2026-06-05,Friday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-05,Friday,Marcus T.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Nina W.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Tomas H.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Tony A.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Marcus T.,Shift Supervisor,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-06,Saturday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Omar T.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Cody M.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Delphine A.,Busser,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Owen K.,Carry Out,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Bella C.,Host,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Dario V.,Host,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Tobias N.,Host,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Xavier R.,Host,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,20000,
2026-06-06,Saturday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-06,Saturday,Dante F.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-06,Saturday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Duncan L.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Farah A.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Nikolai P.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Yara S.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-06,Saturday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-06,Saturday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Raj P.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Chloe B.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Elena V.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Ethan M.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-06,Saturday,James H.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Lena S.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Liam P.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Mason C.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Nina W.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Noah K.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Priya K.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-07,Sunday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-07,Sunday,Marisol T.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,James H.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Sienna P.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Tobias N.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-07,Sunday,Simone R.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Wei C.,Pantry Cook,10:30am,6:30pm,8.0,8.0,12250,
2026-06-07,Sunday,Freya S.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,12250,
2026-06-07,Sunday,Camila G.,Prep Cook,10:00am,5:30pm,7.5,7.5,12250,
2026-06-07,Sunday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-07,Sunday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-07,Sunday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Ava S.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Grace T.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Marcus T.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Owen D.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Sofia R.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-08,Monday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-08,Monday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,Omar T.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-08,Monday,Delphine A.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,James H.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Lena S.,Carry Out,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-08,Monday,Owen K.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Rhea D.,Carry Out,11:00am,5:00pm,6.0,6.0,8000,
2026-06-08,Monday,Aisha K.,Pantry Cook,8:00am,4:00pm,8.0,8.0,8000,
2026-06-08,Monday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Dante F.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Wei C.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Ivy R.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,Miles B.,Pizza Cook,8:00am,4:00pm,8.0,8.0,8000,
2026-06-08,Monday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-08,Monday,Yara S.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-08,Monday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-08,Monday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-08,Monday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-08,Monday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-08,Monday,Diego L.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-08,Monday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Jamie L.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-08,Monday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-08,Monday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Mason C.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Omar T.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Tomas H.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Xavier R.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-09,Tuesday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,9000,
2026-06-09,Tuesday,Carlos B.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Freya S.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,9000,
2026-06-09,Tuesday,Talia D.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,9000,
2026-06-09,Tuesday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,9000,
2026-06-09,Tuesday,Felix G.,Saute Cook,3:00pm,11:00pm,8.0,8.0,9000,
2026-06-09,Tuesday,Theo A.,Saute Cook,5:00pm,11:30pm,6.5,6.5,9000,
2026-06-09,Tuesday,Marcus T.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Nina W.,Server,5:00pm,11:00pm,6.0,6.0,9000,
2026-06-09,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Zoe H.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-10,Wednesday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Sienna P.,Carry Out,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Bella C.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Aisha K.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Bruno T.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-10,Wednesday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-10,Wednesday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-10,Wednesday,Talia D.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,11000,
2026-06-10,Wednesday,Jonah S.,Runner,11:00am,2:30pm,3.5,3.5,11000,
2026-06-10,Wednesday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,11000,
2026-06-10,Wednesday,Chloe B.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,James H.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Maya R.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Rosa M.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Ruby F.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-11,Thursday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-11,Thursday,James H.,Carry Out,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Jonah S.,Host,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Xavier R.,Host,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,13000,
2026-06-11,Thursday,Simone R.,Pantry Cook,8:00am,4:00pm,8.0,8.0,13000,
2026-06-11,Thursday,Duncan L.,Prep Cook,8:00am,3:30pm,7.5,7.5,13000,
2026-06-11,Thursday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,13000,
2026-06-11,Thursday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Theo A.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Ava S.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Gina F.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Grace T.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Marco D.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Owen D.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Marcus T.,Shift Supervisor,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-12,Friday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-12,Friday,Omar T.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Cody M.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-12,Friday,Lena S.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Marisol T.,Carry Out,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Rhea D.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-12,Friday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,James H.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Sienna P.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Wei C.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Freya S.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Yara S.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-12,Friday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-12,Friday,Caleb W.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-12,Friday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-12,Friday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,18000,
2026-06-12,Friday,Ava S.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Bella C.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,18000,
2026-06-12,Friday,Grace T.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Kim T.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Marcus T.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-12,Friday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Noah K.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-12,Friday,Tony A.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-13,Saturday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Rosa M.,Bartender,4:00pm,10:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Delphine A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Marisol T.,Carry Out,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Bella C.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,James H.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Tobias N.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,20000,
2026-06-13,Saturday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Dante F.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Hector M.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Jasper N.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Miles B.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Camila G.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Duncan L.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-13,Saturday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-13,Saturday,Isla V.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Lena S.,Server,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Liam P.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Maya R.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Owen D.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Sienna P.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Sofia R.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-14,Sunday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Rosa M.,Bartender,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,James H.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Xavier R.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Lena S.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Owen K.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Bruno T.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Dante F.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Hector M.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Simone R.,Pantry Cook,10:30am,6:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Miles B.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,12250,
2026-06-14,Sunday,Kenji H.,Prep Cook,8:00am,3:30pm,7.5,7.5,12250,
2026-06-14,Sunday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-14,Sunday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-14,Sunday,Felix G.,Saute Cook,8:30am,4:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Isla V.,Saute Cook,8:30am,4:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Derek M.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Diego L.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Nina W.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Noah K.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Ruby F.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Zoe H.,Server,4:30pm,10:30pm,6.0,6.0,12250,"""

    if not skip_shift_seed:
        # Use save_client_data so the correct DB path is always used
        save_client_data(2, "shifts", gia_mia_csv, source="seed")
        print("[auto-seed] Gia Mia shift data seeded successfully")
    print("[auto-seed] Gia Mia settings always applied")


def _do_seed_gia_mia():
    try:
        from models import create_restaurant, Restaurant
        gia_mia_rid = create_restaurant(Restaurant(
                name="Gia Mia",
                owner_email="cavnarwill@gmail.com",
                owner_name="Brian",
                google_place_id="ChIJpS0UwhwDD4gRVqMFnJkDLvQ",
                neighborhood="St. Charles, Illinois — downtown First Street Plaza",
                vibe="Contemporary Italian pizza bar with wood-fired Neapolitan pizzas and a lively bar scene",
                known_for="Wood-fired Neapolitan pizza, fresh pasta, small plates, craft cocktails, half-price wine Wednesdays, patio dining",
                yelp_business_id="gia-mia-st-charles-st-charles",
                menu_url="https://www.giamiapizzabar.com/menus",
                module_reviews=1, module_labor=1,
                module_inventory=1, module_marketing=1,
                billing_status="trial",
                is_demo=1,
        ))
        gia_mia_pw = os.getenv("RYAN_TEST_PASSWORD", "charthouse123")
        create_user(gia_mia_rid, "Brian", "cavnarwill@gmail.com", gia_mia_pw, is_admin=False)
        print(f"\n  Test client created: Brian / {gia_mia_pw} (Gia Mia, St. Charles IL)\n")

        # Seed labor history FIRST (no API calls, instant) — always replace on fresh deploy
        try:
            from models import save_labor_snapshot as _gm_sls, get_conn as _gc_gm_lh
            # Clear existing labor history for Gia Mia and reseed fresh
            _gm_clh = _gc_gm_lh()
            _gm_clh.execute("DELETE FROM labor_history WHERE restaurant_id=?", (gia_mia_rid,))
            _gm_clh.commit(); _gm_clh.close()
            if True:
                _now_gm = datetime.now()
                _lh_gm = [(-49,34.2,42800,125200),(-42,33.1,44100,133200),(-35,31.8,45600,143400),
                           (-28,32.5,43200,132900),(-21,31.2,46800,150000),(-14,30.8,47200,153200),
                           (-7,30.9,45900,148500),(0,30.9,45900,148500)]
                for _off_gm,_pct_gm,_lab_gm,_sal_gm in _lh_gm:
                    _gm_sls(gia_mia_rid,
                        (_now_gm+timedelta(days=_off_gm)).strftime("%Y-%m-%d"),
                        (_now_gm+timedelta(days=_off_gm+13)).strftime("%Y-%m-%d"),
                        _pct_gm, _lab_gm, _sal_gm)
                print("  Gia Mia labor history seeded.\n")
        except Exception as _lh_gm_e:
            print(f"  Gia Mia labor history seed error: {_lh_gm_e}")

        # Draft responses — hardcoded on Railway (fast), real API locally (quality)
        try:
            _on_railway = config.on_railway()
            if _on_railway:
                _drafts_map = {
                    "positive": [
                        "Thank you so much for the kind words — it truly means the world to our team. We can't wait to welcome you back to the lagoon!",
                        "What a wonderful review — we're so glad you had a great experience. Please come see us again soon!",
                        "This made our whole team smile. Thank you for sharing your experience — see you next time!",
                        "We're so grateful for guests like you. Thank you for the kind review and we hope to see you back very soon!",
                    ],
                    "negative": [
                        "We're truly sorry to hear about your experience and we take this feedback very seriously. Please reach out to us directly at ryans@charthouse.com so we can make this right.",
                        "This is not the standard we hold ourselves to and we sincerely apologize. We'd love the chance to speak with you directly — please contact us at ryans@charthouse.com.",
                        "We're sorry your visit didn't meet expectations. Your feedback has been shared with our management team.",
                    ],
                    "neutral": [
                        "Thank you for taking the time to share your experience. We appreciate the honest feedback and hope to exceed your expectations on your next visit.",
                        "Thanks for visiting and for the thoughtful review. We'd love to show you an even better experience next time.",
                    ],
                }
                _d_idx = {"positive": 0, "negative": 0, "neutral": 0}
                _conn_d = get_conn()
                _pending_d = _conn_d.execute(
                    "SELECT id, sentiment FROM reviews WHERE restaurant_id=? AND (draft_response IS NULL OR draft_response='')",
                    (gia_mia_rid,)
                ).fetchall()
                for _rev_d in _pending_d:
                    _sk = _rev_d["sentiment"] if _rev_d["sentiment"] in _drafts_map else "neutral"
                    _dl = _drafts_map[_sk]
                    _dt = _dl[_d_idx[_sk] % len(_dl)]
                    _d_idx[_sk] += 1
                    _conn_d.execute(
                        "UPDATE reviews SET draft_response=?, response_status='drafted' WHERE id=?",
                        (_dt, _rev_d["id"])
                    )
                _conn_d.commit(); _conn_d.close()
                print("  Gia Mia reviews drafted (hardcoded — Railway).\n")
            else:
                from drafter import draft_pending
                draft_pending(gia_mia_rid, limit=50)
                print("  Gia Mia reviews seeded and drafted (API — local).\n")
        except Exception as _de:
            print(f"  Draft error: {_de}")

        # Seed 6 weeks of inventory history so the trend chart is always populated for testing
        try:
                import json as _json_gm_inv
                from datetime import timedelta as _td_gm
                _gm_inv_weeks = [
                        (241.80, ["Romaine Lettuce", "Bread Rolls", "Roma Tomatoes", "Baby Spinach"]),
                        (218.50, ["Bread Rolls", "Roma Tomatoes", "Sourdough Loaf"]),
                        (309.20, ["Romaine Lettuce", "Bread Rolls", "Salmon Fillet", "Baby Spinach"]),
                        (284.70, ["Roma Tomatoes", "Bread Rolls", "Fresh Herbs Mix"]),
                        (253.10, ["Bread Rolls", "Baby Spinach", "Romaine Lettuce"]),
                        (267.45, ["Romaine Lettuce", "Bread Rolls", "Roma Tomatoes", "Baby Spinach"]),
                ]
                from zoneinfo import ZoneInfo as _ZI_gm_inv
                from datetime import datetime as _dt_gm_inv
                # Use today as week_end anchor — matches how analyse_inventory saves snapshots
                _today_gm = _dt_gm_inv.now(_ZI_gm_inv('America/Chicago')).date()
                _conn_gm_inv = get_conn()
                for _wi, (_waste, _items) in enumerate(_gm_inv_weeks):
                        # Go back 5,4,3,2,1,0 weeks from today — same cadence as real weekly uploads
                        _week_end = (_today_gm - _td_gm(weeks=(5 - _wi))).isoformat()
                        _snap = _json_gm_inv.dumps({"total_waste_cost": _waste, "top_items": _items})
                        _ex = _conn_gm_inv.execute(
                                "SELECT id FROM inventory_history WHERE restaurant_id=? AND week_end=?",
                                (gia_mia_rid, _week_end)
                        ).fetchone()
                        if _ex:
                                _conn_gm_inv.execute(
                                        "UPDATE inventory_history SET waste_json=? WHERE id=?",
                                        (_snap, _ex["id"])
                                )
                        else:
                                _conn_gm_inv.execute(
                                        "INSERT INTO inventory_history (restaurant_id, waste_json, week_end) VALUES (?,?,?)",
                                        (gia_mia_rid, _snap, _week_end)
                                )
                _conn_gm_inv.commit()
                _conn_gm_inv.close()
                print("  Gia Mia inventory trend history seeded (6 weeks).\n")
        except Exception as _gm_inv_e:
                print(f"  Gia Mia inventory seed error: {_gm_inv_e}")

        # Seed rich inventory CSV for Gia Mia so the order list shows all row types
        try:
                from models import save_client_data as _scd_gm
                _gm_inv_csv = """item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week
Chilean Sea Bass,Protein,lb,12,2,28.50,2.2,12,0.5
Prime Rib,Protein,lb,20,3,18.75,3.8,20,1.2
Lobster Tail,Protein,lb,8,1,42.00,1.4,8,0.3
Shrimp 16/20,Protein,lb,15,6,14.20,2.6,15,1.8
Salmon Fillet,Protein,lb,14,10,16.50,2.1,14,2.4
Chicken Breast,Protein,lb,18,22,5.80,2.8,18,0.6
Filet Mignon,Protein,lb,10,12,32.00,1.5,10,0.4
Romaine Lettuce,Produce,head,24,30,2.50,3.2,24,9.5
Roma Tomatoes,Produce,lb,16,22,1.80,2.4,20,7.2
Baby Spinach,Produce,lb,10,14,4.20,1.2,10,4.8
Fresh Herbs Mix,Produce,bunch,6,8,5.50,0.6,6,3.1
Lemons,Produce,each,40,18,0.60,5.5,40,2.0
Asparagus,Produce,lb,12,9,3.80,1.8,12,1.1
Russet Potatoes,Produce,lb,25,12,0.80,4.2,25,3.5
Heavy Cream,Dairy,qt,10,16,3.80,1.4,10,0.8
Butter Unsalted,Dairy,lb,12,18,4.50,1.6,12,0.4
Parmesan Cheese,Dairy,lb,6,9,8.20,0.8,6,0.6
Bread Rolls,Bakery,each,80,52,0.45,14.0,80,22.0
Sourdough Loaf,Bakery,loaf,20,26,3.20,3.0,20,8.5
Pasta Rigatoni,Pantry,lb,18,22,2.80,2.5,18,1.2
Olive Oil Extra Virgin,Pantry,bottle,8,11,14.50,0.9,8,0.2
Beef Stock,Pantry,qt,10,14,4.80,1.4,10,0.3
White Wine Chardonnay,Beverage,bottle,16,20,8.50,2.0,16,0.0
House Cabernet,Beverage,bottle,20,24,9.20,2.8,20,0.0
Sparkling Water,Beverage,case,6,9,22.00,0.8,6,0.0"""
                _scd_gm(gia_mia_rid, "inventory", _gm_inv_csv, source="upload")
                print("  Gia Mia rich inventory CSV seeded.\n")
        except Exception as _gm_csv_e:
                print(f"  Gia Mia inventory CSV seed error: {_gm_csv_e}")


    except Exception as _do_seed_gm_e:
        print(f"  Gia Mia full seed error: {_do_seed_gm_e}")

def _seed_gia_mia_background():
    try:
        import time as _t_gm
        # Wait for DB to be ready — poll instead of blind sleep
        for _attempt in range(10):
            try:
                _test = get_conn()
                _test.execute("SELECT 1").fetchone()
                _test.close()
                break
            except Exception:
                _t_gm.sleep(1)
        conn = get_conn()
        gia_mia_exists = conn.execute(
            """SELECT u.id FROM users u
               JOIN restaurants r ON u.restaurant_id=r.id
               WHERE r.name=? AND u.is_admin=0""",
            ("Gia Mia",)
        ).fetchone()
        conn.close()
        if not gia_mia_exists:
            _do_seed_gia_mia()
        else:
            _ensure_gia_mia_vibe()
        # Refresh seed reviews on every deploy so demo content stays current —
        # gated on is_demo (not just the name match) so this permanently stops
        # touching Gia Mia's data the moment it's flipped off in admin, e.g.
        # if/when Gia Mia converts from a pitched demo to a real paying client.
        conn2 = get_conn()
        _gia_mia_row = conn2.execute(
            "SELECT id, is_demo FROM restaurants WHERE name=?", ("Gia Mia",)
        ).fetchone()
        _gia_mia_is_demo = bool(_gia_mia_row["is_demo"]) if _gia_mia_row else False
        # The flag is the only gate. It used to be re-set to 1 by name on
        # every boot ("one-time backfill"), which meant turning it off in the
        # console never stuck and the next deploy wiped the reviews again.
        conn2.close()
        if _gia_mia_row and _gia_mia_is_demo:
            _refresh_gia_mia_reviews(_gia_mia_row["id"])
            _refresh_gia_mia_value_history(_gia_mia_row["id"])
    except Exception as _bg_gm_e:
        print(f"  Gia Mia seed background error: {_bg_gm_e}")
    # Seed Gia Mia demo data after seed completes (avoids concurrent DB writes)
    try:
        _auto_seed_demo_clients()
    except Exception as _gm_e:
        print(f"  Gia Mia auto-seed error: {_gm_e}")


def _ensure_gia_mia_vibe():
    """Backfills owner_email/owner_name/vibe/known_for on the demo restaurant
    only when at least one is genuinely missing — used to run unconditionally
    on every server restart, which meant any admin-panel edit to these exact
    fields (all editable from client_settings.html) got silently reverted
    back to these hardcoded values on the next deploy."""
    try:
        conn = get_conn()
        # Search by restaurant name — avoids colliding with admin's will@cavnar.ai
        row = conn.execute(
            "SELECT id, owner_email, owner_name, vibe, known_for FROM restaurants WHERE name=?", ("Gia Mia",)
        ).fetchone()
        if row and not all([row["owner_email"], row["owner_name"], row["vibe"], row["known_for"]]):
            conn.execute("""
                UPDATE restaurants SET
                    owner_email=?, owner_name=?,
                    vibe=?, known_for=?
                WHERE id=?
            """, (
                "cavnarwill@gmail.com", "Brian",
                "Contemporary Italian pizza bar with wood-fired Neapolitan pizzas and a lively bar scene",
                "Wood-fired Neapolitan pizza, fresh pasta, small plates, craft cocktails, half-price wine Wednesdays, patio dining",
                row["id"],
            ))
            conn.execute(
                "UPDATE users SET email=?, username=? WHERE restaurant_id=? AND is_admin=0",
                ("cavnarwill@gmail.com", "brian", row["id"])
            )
            conn.commit()
            print(f"  Gia Mia profile backfilled (email, owner, vibe, known_for) on restaurant id={row['id']}")
        conn.close()
    except Exception as _vibe_e:
        print(f"  Gia Mia vibe ensure error: {_vibe_e}")


def _refresh_gia_mia_reviews(gia_mia_rid):
    """Always re-seed the 90 real Gia Mia reviews on every deploy so content stays current.

    Weekly volume is deliberately uneven (5-20 reviews/week) — real review
    inflow is bursty (weekend rushes, a good night that gets talked about,
    a slow week), not a flat constant. An earlier version of this seed had
    exactly 4 reviews every single week, which made the Reviews chart's
    weekly trend look fake/flat regardless of what the chart itself did.
    """
    try:
        import json as _json_r
        from zoneinfo import ZoneInfo as _ZI_r
        from datetime import datetime as _dt_r, timedelta as _td_r2
        conn = get_conn()
        # Wipe seeded reviews — draft_response is a column on reviews, so this covers everything
        conn.execute("DELETE FROM reviews WHERE restaurant_id=? AND external_id LIKE 'rr_%'", (gia_mia_rid,))
        conn.commit()

        # Rating distribution: 61×5★, 19×4★, 5×3★, 5×1★ → avg 4.46 (matches real 4.5 Google rating)
        # Real complaints kept — just corrected star ratings to match TripAdvisor actuals (3-4★ for issues, 1★ only for phone rudeness)
        sample_reviews = [
                # Week 8 (oldest, -49 days) — 5 reviews
                ("google","rr_w8a",5,"The quattro formaggi pizza here is extraordinary — perfectly balanced with an extra crispy wood-fired crust. Sat on the patio on a Friday evening and the vibe was fantastic. Will be back weekly if I could.","positive","Karen B.",["food_quality","ambiance"],"normal"),
                ("google","rr_w8b",5,"I LOVE their Margherita Pizza and beet salad! Simple, fresh, and done right. Great spot downtown St. Charles.","positive","Jenna L.",["food_quality"],"normal"),
                ("yelp",  "rr_w8c",3,"The food was great — really enjoyed the pizza and the patio. But the music inside is so loud you can't hold a conversation. Would only sit outside. Still worth going for the food, just know what you're walking into noise-wise.","neutral","Patricia M.",["ambiance","food_quality"],"normal"),
                ("google","rr_w8d",5,"Solid wood-fired pizza and great atmosphere downtown. The pasta was excellent — really fresh. Love this spot for a casual dinner or date night. One of the best in St. Charles.","positive","Steven R.",["food_quality","ambiance"],"normal"),
                ("google","rr_w8e",5,"Wood-fired pizza and a cozy patio — exactly what we wanted for a Friday date night. The pear pizza is incredible.","positive","Megan P.",["food_quality","ambiance"],"normal"),
                # Week 7 (-42 days) — 12 reviews
                ("google","rr_w7a",5,"The meatballs al forno are AMAZING — five stars on their own. Served on polenta with tomato sauce, just perfect. The chef was also willing to modify dishes for our vegan friend which we really appreciated.","positive","Michelle H.",["food_quality","service"],"normal"),
                ("google","rr_w7b",5,"Pear pizza with caramelized onions is one of the best things I've eaten. Came in for wine Wednesday half-price deal and left very happy. The craft cocktails are also excellent.","positive","Donald C.",["food_quality","value"],"normal"),
                ("yelp",  "rr_w7c",1,"Tried calling to ask about a reservation — called five times over three days. No answer. When someone finally picked up they were short and rude. Won't be making a reservation there.","negative","Sandra W.",["service"],"high"),
                ("google","rr_w7d",4,"Really enjoyed our dinner here. The shrimp and polenta appetizer had incredible flavor and a very generous portion. Service was a little slow to start but attentive once they found us. Good spot for a date night.","positive","Gary L.",["food_quality","ambiance","service"],"normal"),
                ("google","rr_w7e",5,"The meatballs al forno alone are worth the trip. Polenta base is perfect, sauce is rich without being heavy.","positive","Chris D.",["food_quality"],"normal"),
                ("yelp",  "rr_w7f",5,"Wine Wednesday is the best deal in St. Charles. Half-price bottles and the pear pizza is unbeatable.","positive","Ashley N.",["value","food_quality"],"normal"),
                ("google","rr_w7g",5,"Consistently great pizza night after night. The quattro formaggi never disappoints.","positive","Brandon K.",["food_quality"],"normal"),
                ("google","rr_w7h",4,"Good food, good patio, a little pricey for the portion sizes but I'd still recommend it.","positive","Nicole F.",["food_quality","value"],"normal"),
                ("yelp",  "rr_w7i",5,"Took my in-laws here and they were blown away by the wood-fired crust. Will be back for sure.","positive","Justin R.",["food_quality"],"normal"),
                ("google","rr_w7j",3,"Food was good but we waited almost 20 minutes just to get water after being seated. Kitchen seemed backed up.","neutral","Katie S.",["service","wait_time"],"normal"),
                ("google","rr_w7k",5,"Best patio in downtown St. Charles, hands down. Perfect spot for a warm evening with friends.","positive","Ryan B.",["ambiance"],"normal"),
                ("yelp",  "rr_w7l",5,"The beet salad is way better than it sounds — go in with an open mind. Paired perfectly with the margherita.","positive","Emily V.",["food_quality"],"normal"),
                # Week 6 (-35 days) — 8 reviews
                ("google","rr_w6a",5,"Our go-to in St. Charles. Came with a group of 8 and we were seated quickly, food came out fast, and every pizza was spot on. Great for larger parties.","positive","Nancy P.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w6b",4,"Really solid Italian. The fresh pasta dishes are excellent and the wood-fired pizza has the perfect char. Wine Wednesday is a steal. Love sitting on the patio. Music inside is a bit loud but the food more than makes up for it.","positive","Kevin S.",["food_quality","value","ambiance"],"normal"),
                ("google","rr_w6c",3,"Had a rough night — order came out wrong and the utensils weren't clean when they arrived. Staff replaced everything without much fuss. The pizza itself was great, but the service execution left something to be desired.","neutral","Betty A.",["service","cleanliness","food_quality"],"normal"),
                ("yelp",  "rr_w6d",5,"The food here is just really good Italian — wood-fired pizza with great char, fresh pasta, solid small plates. It's a set menu so no customization, but everything on it is worth ordering. Patio is beautiful.","positive","Brian N.",["food_quality","ambiance"],"normal"),
                ("google","rr_w6e",5,"Everything about this place screams quality — the crust, the sauce, the service. A neighborhood favorite for good reason.","positive","Jason T.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w6f",5,"Had the pear pizza and a bottle of wine on the patio — genuinely one of our best dinners this year.","positive","Laura M.",["food_quality","ambiance"],"normal"),
                ("google","rr_w6g",4,"Great pizza as always. Wish they took reservations for smaller parties too, we had a short wait on a Saturday.","positive","Eric H.",["food_quality","wait_time"],"normal"),
                ("google","rr_w6h",5,"Fresh pasta was a standout — didn't expect it to be as good as the pizza, but it was.","positive","Amber C.",["food_quality"],"normal"),
                # Week 5 (-28 days) — 20 reviews
                ("google","rr_w5a",5,"Really great food and incredibly fast for a Friday night. Came with 8 people, had a time schedule, and they got us in and out in under 30 minutes without rushing us. Impressive.","positive","Dorothy K.",["food_quality","service"],"normal"),
                ("google","rr_w5b",5,"Perfect patio dining — beautiful summer evening, excellent wood-fired pizza, strong cocktail list. The kind of spot you bring out-of-town guests. Can't recommend enough.","positive","Charles V.",["food_quality","ambiance"],"normal"),
                ("yelp",  "rr_w5c",5,"Slow start getting seated but the meatballs al forno absolutely made up for it — one of the best bites I've had in St. Charles. Pizza was also excellent. Worth every minute of the wait.","positive","Helen J.",["food_quality","service"],"normal"),
                ("google","rr_w5d",5,"The wood-fired pizza here is the real deal — charred perfectly, fresh toppings, light and delicious. The patio is the move in summer. Solid cocktail program too. A genuine neighborhood gem.","positive","Frank M.",["food_quality","ambiance"],"normal"),
                ("google","rr_w5e",5,"The quattro formaggi is genuinely one of the best pizzas I've had anywhere, not just St. Charles.","positive","Tyler J.",["food_quality"],"normal"),
                ("yelp",  "rr_w5f",5,"Patio was full and lively but they still got our food out fast. Impressed with how well they handle a packed house.","positive","Stephanie L.",["food_quality","service"],"normal"),
                ("google","rr_w5g",5,"Came for wine Wednesday and stayed for three hours. Great food, great wine list, great people watching from the patio.","positive","Brett O.",["value","ambiance"],"normal"),
                ("google","rr_w5h",4,"Really good overall. Docking one star only because the inside gets loud — sit on the patio if you can.","positive","Danielle W.",["ambiance","food_quality"],"normal"),
                ("yelp",  "rr_w5i",5,"Meatballs al forno are a must-order. My whole table agreed it was the best dish of the night.","positive","Kyle P.",["food_quality"],"normal"),
                ("google","rr_w5j",5,"This is our go-to for celebrations. Never had a bad meal here in two years of coming.","positive","Melissa G.",["food_quality"],"normal"),
                ("google","rr_w5k",3,"Solid food but the AC seemed to be struggling on a hot night — pretty warm inside. Patio was fine though.","neutral","Sean R.",["ambiance"],"normal"),
                ("yelp",  "rr_w5l",5,"The pear and caramelized onion pizza is unreal. Ordered a second one to take home.","positive","Christina B.",["food_quality"],"normal"),
                ("google","rr_w5m",5,"Downtown St. Charles doesn't get better than this for a casual Italian night out.","positive","Adam F.",["food_quality","ambiance"],"normal"),
                ("google","rr_w5n",4,"Great pizza, solid cocktails, service was a touch slow but everyone was clearly working hard on a busy night.","positive","Rebecca T.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w5o",5,"Brought the whole extended family for a birthday dinner and they nailed it for a group of ten.","positive","Jacob N.",["food_quality","service"],"normal"),
                ("google","rr_w5p",5,"Perfect summer patio spot. Great wine list, even better pizza.","positive","Vanessa D.",["ambiance","food_quality"],"normal"),
                ("google","rr_w5q",5,"The shrimp and polenta appetizer is criminally underrated — order it before it sells out.","positive","Ian S.",["food_quality"],"normal"),
                ("yelp",  "rr_w5r",1,"Sat outside for 15 minutes with no menus and no acknowledgment. Left and went elsewhere. Disappointing given how good I remember the food being.","negative","Courtney M.",["service"],"high"),
                ("google","rr_w5s",5,"Fresh, flavorful, and the patio at sunset is unbeatable. Highly recommend for date night.","positive","Marcus H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w5t",4,"Very good pizza and a nice wine selection. Would've given 5 stars if the wait for a table were shorter on a Friday.","positive","Renee A.",["food_quality","wait_time"],"normal"),
                # Week 4 (-21 days) — 6 reviews
                ("google","rr_w4a",5,"Celebrated my wife's birthday here and the whole experience was wonderful. Staff was warm, food was incredible — the wood-fired Neapolitan pizza is the real deal. This place is special.","positive","Ruth C.",["food_quality","service","ambiance"],"normal"),
                ("yelp",  "rr_w4b",5,"Best Italian pizza bar in the Fox Valley, no contest. The quattro formaggi and the pear caramelized onion pizza are both outstanding. Outdoor patio is gorgeous. We come every month.","positive","Edward H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w4c",4,"Very busy Valentine's Day — server was stretched thin all night but doing their best. The food was absolutely delicious as always. The kitchen delivered even under pressure. Food 5/5, service takes the hit on a night like that.","positive","Carol D.",["food_quality","service","wait_time"],"normal"),
                ("yelp",  "rr_w4d",5,"Wine Wednesday half-price is a genuine deal — came with three friends, had a fantastic evening. Small plates are perfect for sharing, bar scene is lively, patio was beautiful. One of the best midweek dinner spots around.","positive","Mark S.",["food_quality","value","ambiance"],"normal"),
                ("google","rr_w4e",5,"The quattro formaggi pizza is the best in the Fox Valley, hands down. Perfect char every time.","positive","Todd B.",["food_quality"],"normal"),
                ("yelp",  "rr_w4f",4,"Good date night spot. A little loud inside but the patio more than makes up for it.","positive","Heather L.",["ambiance"],"normal"),
                # Week 3 (-14 days) — 16 reviews
                ("google","rr_w3a",5,"Fresh pasta, meatballs al forno, wood-fired pizza — everything we ordered was outstanding. Service was attentive and the patio vibe on a warm evening is unbeatable. One of the best restaurants in St. Charles.","positive","Linda F.",["food_quality","service","ambiance"],"normal"),
                ("google","rr_w3b",5,"Downtown location is perfect and the food backs it up completely. The pizza is always excellent — crispy crust, fresh ingredients, not too heavy. Patio in the summer is wonderful. A true gem.","positive","Paul B.",["food_quality","ambiance"],"normal"),
                ("yelp",  "rr_w3c",4,"Service was a bit slow — took a while to get water and our server was handling too many tables. But the food absolutely made up for it. Pizza was incredible and the patio atmosphere is hard to beat. We'll definitely be back.","positive","Barbara G.",["food_quality","service","ambiance"],"normal"),
                ("google","rr_w3d",4,"Reliable and generally excellent. The pizza is always on point — my go-to is the quattro formaggi. Some pasta dishes can be inconsistent but when they're on, they're really good. Great patio and atmosphere.","positive","Thomas E.",["food_quality","ambiance"],"normal"),
                ("google","rr_w3e",5,"Every visit is consistent — great pizza, great patio, friendly staff. Never had a bad experience here.","positive","Victor M.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w3f",5,"The pear pizza with caramelized onions might be the best pizza I've had in Illinois.","positive","Samantha C.",["food_quality"],"normal"),
                ("google","rr_w3g",5,"Fantastic spot for a group dinner. They handled our party of six without any issues.","positive","Wesley P.",["food_quality","service"],"normal"),
                ("google","rr_w3h",4,"Really solid meal. Wish the cocktail menu had a bit more variety but everything we tried was well made.","positive","Diane R.",["food_quality"],"normal"),
                ("yelp",  "rr_w3i",5,"Wine Wednesday plus the meatballs al forno is the perfect combo. Can't recommend it enough.","positive","Grant S.",["value","food_quality"],"normal"),
                ("google","rr_w3j",5,"This is the best pizza in downtown St. Charles, full stop. Patio seating makes it even better.","positive","Monica T.",["food_quality","ambiance"],"normal"),
                ("google","rr_w3k",1,"Called three separate times trying to book a table for a birthday and never got a callback. Ended up going elsewhere.","negative","Phillip D.",["reservation","service"],"high"),
                ("yelp",  "rr_w3l",5,"The fresh pasta specials rotate and they're always excellent. Ask your server what's on that night.","positive","Erin K.",["food_quality"],"normal"),
                ("google","rr_w3m",5,"Great neighborhood Italian spot. The wood-fired oven really makes a difference in the crust.","positive","Bruce N.",["food_quality"],"normal"),
                ("google","rr_w3n",4,"Good overall experience. Service was attentive, food came out quickly, just wish portions were a bit bigger for the price.","positive","Jasmine V.",["food_quality","value"],"normal"),
                ("yelp",  "rr_w3o",5,"Sat on the patio for two hours and never felt rushed. Exactly the kind of relaxed dinner we wanted.","positive","Colin F.",["ambiance","service"],"normal"),
                ("google","rr_w3p",5,"The quattro formaggi and a glass of red wine is basically a perfect evening. Highly recommend.","positive","Natalie H.",["food_quality"],"normal"),
                # Week 2 (-7 days) — 10 reviews
                ("google","rr_w2a",5,"The wood-fired Neapolitan pizza here is extraordinary — literally a 10/10. Love the location, love the service, love the atmosphere. Sat outside on the patio and it was a perfect evening. Highly, highly recommend.","positive","Jennifer M.",["food_quality","service","ambiance"],"normal"),
                ("google","rr_w2b",4,"Took a bit to get acknowledged after being seated — no menus for a while. Once our server arrived everything was great. The pizza and pasta were both excellent. Worth the slightly slow start, will return.","positive","David K.",["food_quality","service","wait_time"],"normal"),
                ("yelp",  "rr_w2c",5,"Came for our anniversary dinner and it was perfect. The shrimp and polenta appetizer is rich and delicious — generous portion too. Patio was stunning. This is our new favorite spot in St. Charles.","positive","Sarah T.",["food_quality","ambiance"],"normal"),
                ("google","rr_w2d",5,"Great spot for a weeknight dinner. The beet salad and margherita pizza combo is simple and delicious. Friendly staff, beautiful patio, solid cocktails. Exactly what you want from a neighborhood Italian.","positive","Mike R.",["food_quality","ambiance","service"],"normal"),
                ("google","rr_w2e",5,"Great weeknight dinner spot. The pizza is consistently excellent and the staff is always friendly.","positive","Douglas M.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w2f",5,"The patio is gorgeous in the evening and the food matches the setting. Will be back soon.","positive","Alicia P.",["ambiance","food_quality"],"normal"),
                ("google","rr_w2g",4,"Good pizza, solid service. Would've been 5 stars but we waited a bit for our check at the end.","positive","Marcus B.",["food_quality","service"],"normal"),
                ("google","rr_w2h",5,"Best Italian in St. Charles. The meatballs al forno are a must-try if you haven't already.","positive","Kimberly R.",["food_quality"],"normal"),
                ("yelp",  "rr_w2i",3,"Food was good but it was very loud inside on a Saturday night — hard to hold a conversation. Patio would've been better.","neutral","Trevor S.",["ambiance"],"normal"),
                ("google","rr_w2j",5,"Perfect spot for a casual date night. Great wine list and even better pizza.","positive","Olivia K.",["food_quality","ambiance"],"normal"),
                # Week 1 (most recent) — 13 reviews
                ("yelp",  "rr_w1a",1,"Called to make a reservation and finally got through on my fifth attempt over three days. The person who answered was short and borderline rude. Won't bother — plenty of other Italian restaurants that actually want our business.","negative","Amanda L.",["service"],"high"),
                ("google","rr_w1b",5,"Gia Mia is consistently excellent. The wood-fired crust is perfect every single time — charred just right, never soggy. Patio dining in the summer is the move. Our family's go-to for special occasions and casual Wednesdays alike.","positive","Robert H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w1c",4,"Pizza is genuinely outstanding — no complaints there at all. It gets loud inside so sit outside when weather allows. The patio is lovely. Would recommend for anyone who loves good wood-fired Neapolitan pizza.","positive","Lisa C.",["food_quality","ambiance"],"normal"),
                ("yelp",  "rr_w1d",5,"Outdoor patio is absolutely stunning, especially on a warm evening. Had the meatballs al forno and the quattro formaggi pizza — both incredible. Fresh pasta was also excellent. One of the best Italian spots in the western suburbs.","positive","Tom W.",["food_quality","ambiance"],"normal"),
                ("google","rr_w1e",5,"Consistently the best pizza in the area. We come at least twice a month and it's never disappointed.","positive","Patrick N.",["food_quality"],"normal"),
                ("yelp",  "rr_w1f",5,"The patio is my favorite in downtown St. Charles. Great food to match a great setting.","positive","Angela F.",["ambiance","food_quality"],"normal"),
                ("google","rr_w1g",4,"Really good dinner. Only complaint is it can get loud inside — ask for patio seating if the weather's nice.","positive","Bradley H.",["ambiance"],"normal"),
                ("google","rr_w1h",5,"The quattro formaggi is worth the visit alone. Perfectly crisp crust every single time.","positive","Cassandra L.",["food_quality"],"normal"),
                ("yelp",  "rr_w1i",5,"Wine Wednesday is a genuinely great deal. We plan our week around it sometimes.","positive","Nathaniel P.",["value"],"normal"),
                ("google","rr_w1j",4,"Solid meal, friendly staff. Wish they took reservations for parties under six, we had a short wait.","positive","Jacqueline D.",["service","wait_time"],"normal"),
                ("google","rr_w1k",5,"Best pizza in the Fox Valley area, no debate. The pear pizza is a revelation.","positive","Corey M.",["food_quality"],"normal"),
                ("yelp",  "rr_w1l",1,"Reservation line went unanswered again — third time this has happened to us. Food is great but the phone situation needs fixing.","negative","Miranda T.",["reservation","service"],"high"),
                ("google","rr_w1m",4,"Good experience overall. The patio was full so we sat inside, which was fine but noticeably louder.","positive","Shane K.",["ambiance"],"normal"),
        ]
        _now_r = _dt_r.now(_ZI_r('America/Chicago'))
        _wk_map = {"w8":-49,"w7":-42,"w6":-35,"w5":-28,"w4":-21,"w3":-14,"w2":-7,"w1":0}
        for platform, ext_id, rating, text, sentiment, name, cats, urgency in sample_reviews:
            _wk = ext_id[3:5]
            _offset = _wk_map.get(_wk, 0)
            _rev_dt = (_now_r + _td_r2(days=_offset)).strftime('%Y-%m-%dT%H:%M:%S')
            conn.execute("""
                INSERT OR REPLACE INTO reviews
                (restaurant_id, platform, external_id, author, rating, text, sentiment,
                    categories, urgency, fetched_at, review_date, response_status, processed, review_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (gia_mia_rid, platform, ext_id, name, rating, text, sentiment,
                        _json_r.dumps(cats), urgency, _rev_dt, _rev_dt, "pending", 1, name))
        conn.commit()
        conn.close()
        print("  Gia Mia reviews refreshed with real content (90 reviews).\n")
    except Exception as _rr_e:
        print(f"  Gia Mia review refresh error: {_rr_e}")


def _refresh_gia_mia_value_history(gia_mia_rid):
    """Backfills 90 days of value_snapshots with an organic upward trend.

    record_value_snapshot() (value_delivered.py) only ever records TODAY's
    live-computed total, opportunistically, whenever the mobile Home tab
    loads — there's no historical backfill. For a demo restaurant tested
    repeatedly in a single day, compute_total_value_delivered() barely
    moves day to day (reviews_value only grows when reviews actually get
    responded to), so the real accumulated history ends up nearly flat —
    confirmed directly: 9 straight days at the same value before this fix.
    That's what made the Home tab's "value delivered" sparkline look like
    a flat/straight line instead of a real trend.

    Walks backward from today's actual live total in strictly positive,
    varying steps, so the rightmost point always matches whatever
    compute_total_value_delivered() returns live elsewhere on the same
    screen. Wipes and reseeds every deploy, same as _refresh_gia_mia_reviews.

    DEMO ONLY, and the is_demo flag is the only gate (see the caller). This
    is seeded content in the same sense the demo reviews are — it is not a
    measurement and must never run for a paying account.

    The monotonic assumption behind the upward walk was written for the old
    value figure, where past review responses and months-since-signup could
    only ever accumulate. Since the ROI audit the figure is measured
    outcomes, which genuinely can fall — a tracker can be abandoned, and a
    per-month win figure moves when it is re-measured. The walk stays
    upward because that is what a demo curve should look like, not because
    the underlying metric is monotonic any more.
    """
    try:
        import random as _rand_vh
        from datetime import date as _date_vh, timedelta as _td_vh
        from value_delivered import compute_total_value_delivered as _ctvd

        conn = get_conn()
        conn.execute("DELETE FROM value_snapshots WHERE restaurant_id=?", (gia_mia_rid,))

        current_total = _ctvd(gia_mia_rid) or 0
        if current_total <= 0:
            conn.commit()
            conn.close()
            return

        rng = _rand_vh.Random(20260827)
        today = _date_vh.today()
        points = []
        value = current_total
        d = today
        while (today - d).days < 90:
            points.append((d, value))
            step_back = rng.choice([2, 2, 3, 3, 4])
            d = d - _td_vh(days=step_back)
            # Strictly positive and widely varying — a real restaurant's
            # cumulative value grows in uneven bursts (a batch of review
            # responses, a month of active marketing ticking over), not a
            # smooth ramp, but it never runs backward.
            growth = rng.choice([0, 0, 5, 10, 15, 25, 40, 60, 90, 130, 180, 240])
            value = max(30, value - growth)
        points.reverse()

        for pd, pv in points:
            conn.execute("""
                INSERT INTO value_snapshots (restaurant_id, snapshot_date, total_value)
                VALUES (?, ?, ?)
                ON CONFLICT(restaurant_id, snapshot_date) DO UPDATE SET total_value = excluded.total_value
            """, (gia_mia_rid, pd.isoformat(), pv))
        conn.commit()
        conn.close()
        print(f"  Gia Mia value history backfilled ({len(points)} points, ${points[0][1]} -> ${points[-1][1]}).\n")
    except Exception as _vh_e:
        print(f"  Gia Mia value history refresh error: {_vh_e}")


def start_background_seed():
    """Seed and refresh the demo accounts off the request path. Called once
    from hosted_dashboard at boot; the thread waits for the database."""
    threading.Thread(target=_seed_gia_mia_background, daemon=True).start()
