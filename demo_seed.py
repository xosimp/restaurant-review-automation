"""
demo_seed.py — the demo account (Simple EJ's) and how it is kept fresh.

This used to be ~1,000 lines inside models.py and ~460 inside the app
entry point. Nothing here runs for a real client: every function is gated
on the account's name or its `is_demo` flag, and the admin console's
"reseed demo data" refuses a restaurant that is not flagged.

Entry points:
  start_background_seed()   — hosted_dashboard calls this once at boot
  _seed_simple_ejs          — tests; also reachable as models._seed_simple_ejs

The Gia Mia demo account was removed on 9/25/26 (never a client). Its seed
recreated the account on every boot whenever its login was missing, so it
must not come back: tests/test_demo_flag.py asserts it stays gone.
"""
import json
import threading
from datetime import timedelta

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
        _seed_simple_ejs()
    except Exception as e:
        print(f"[auto-seed] Simple EJ's seed failed: {e}")


# ── Simple EJ's — the working demo account ────────────────────────────────
#
# EVERY NUMBER BELOW IS A PLACEHOLDER. It is a plausible mid-size bar and
# grill, not Erik's real operation, and it is flagged as such in the
# restaurant's internal notes so nobody mistakes it for confirmed data.
# Replace it with his real CSV and hours the moment you have them.
SIMPLE_EJS_NAME = "Simple EJ's"
SIMPLE_EJS_EMAIL = "cavnarwill@gmail.com"

_EJS_ROSTER = [
    # name, role, operational score, typical start, typical end, hours.
    # 55 people (owner, 9/26/26: Erik's real headcount; three managers, so
    # open to close seven days fits without overtime), the first 19 the
    # original demo roster so ratings and history on those names carry on.
    # A shift of 7h or more is a full-timer (five days a week at most); a
    # shorter one is part-time (three) — the history below keeps everybody
    # at or under 40h, the way a manager who avoids overtime writes a week.
    ("Marcus R.", "Bartender", 5, "3:00pm", "11:00pm", 8.0),
    ("Devon K.",  "Bartender", 4, "4:00pm", "11:30pm", 7.5),
    ("Priya S.",  "Bartender", 3, "4:00pm", "10:30pm", 6.5),
    ("Cole T.",   "Bartender", 2, "5:00pm", "11:00pm", 6.0),
    ("Angela M.", "Server",    5, "10:30am", "5:00pm", 6.5),
    ("Reuben O.", "Server",    4, "4:00pm", "10:00pm", 6.0),
    ("Hana W.",   "Server",    3, "11:00am", "5:00pm", 6.0),
    ("Trey B.",   "Server",    3, "4:30pm", "10:30pm", 6.0),
    ("Simone A.", "Server",    2, "5:00pm", "10:00pm", 5.0),
    ("Vince L.",  "Line Cook", 5, "2:00pm", "10:00pm", 8.0),
    ("Omar H.",   "Line Cook", 4, "3:00pm", "11:00pm", 8.0),
    ("Bea C.",    "Line Cook", 3, "3:00pm", "10:30pm", 7.5),
    ("Nico F.",   "Line Cook", 1, "4:00pm", "10:00pm", 6.0),
    ("Jules P.",  "Prep Cook", 4, "8:00am", "3:30pm", 7.5),
    ("Ari D.",    "Prep Cook", 3, "8:00am", "3:00pm", 7.0),
    ("Tessa G.",  "Host",      4, "4:00pm", "10:00pm", 6.0),
    ("Milo J.",   "Host",      3, "11:00am", "4:30pm", 5.5),
    ("Kase N.",   "Busser",    3, "4:30pm", "10:30pm", 6.0),
    ("Lupe V.",   "Busser",    2, "4:30pm", "10:30pm", 6.0),
    ("Dana S.",   "Manager",   5, "10:00am", "6:00pm", 8.0),
    ("Luis G.",   "Manager",   5, "4:00pm", "12:00am", 8.0),
    ("Jade F.",   "Bartender", 4, "3:00pm", "11:00pm", 8.0),
    ("Owen P.",   "Bartender", 3, "5:00pm", "11:30pm", 6.5),
    ("Rita M.",   "Bartender", 3, "11:00am", "5:00pm", 6.0),
    ("Sam K.",    "Bartender", 2, "6:00pm", "12:00am", 6.0),
    ("Carla D.",  "Server",    5, "4:00pm", "11:00pm", 7.0),
    ("Ben Y.",    "Server",    4, "10:30am", "5:30pm", 7.0),
    ("Mia L.",    "Server",    4, "4:00pm", "11:00pm", 7.0),
    ("Theo R.",   "Server",    3, "11:00am", "4:30pm", 5.5),
    ("Nadia P.",  "Server",    3, "5:00pm", "10:30pm", 5.5),
    ("Grant W.",  "Server",    3, "4:30pm", "11:30pm", 7.0),
    ("Ivy C.",    "Server",    2, "11:00am", "4:00pm", 5.0),
    ("Jonah E.",  "Server",    2, "5:00pm", "10:00pm", 5.0),
    ("Lena B.",   "Server",    4, "10:30am", "5:30pm", 7.0),
    ("Parker S.", "Manager",   4, "4:00pm", "12:00am", 8.0),
    ("Zoe H.",    "Server",    3, "4:00pm", "10:00pm", 6.0),
    ("Diego M.",  "Line Cook", 4, "10:00am", "6:00pm", 8.0),
    ("Ray T.",    "Line Cook", 4, "3:00pm", "11:00pm", 8.0),
    ("Kofi A.",   "Line Cook", 3, "10:30am", "5:30pm", 7.0),
    ("Hector L.", "Line Cook", 3, "4:00pm", "12:00am", 8.0),
    ("Sean O.",   "Line Cook", 2, "5:00pm", "11:00pm", 6.0),
    ("Andre B.",  "Line Cook", 2, "11:00am", "5:00pm", 6.0),
    ("Maria V.",  "Prep Cook", 4, "7:00am", "3:00pm", 8.0),
    ("Tomas R.",  "Prep Cook", 2, "9:00am", "2:00pm", 5.0),
    ("Lily A.",   "Host",      3, "10:30am", "4:30pm", 6.0),
    ("Noah C.",   "Host",      2, "5:00pm", "10:00pm", 5.0),
    ("Ella G.",   "Host",      3, "4:00pm", "10:00pm", 6.0),
    ("Chris V.",  "Host",      2, "11:00am", "4:00pm", 5.0),
    ("Eddie F.",  "Busser",    3, "11:00am", "5:00pm", 6.0),
    ("Rosa N.",   "Busser",    3, "5:00pm", "11:00pm", 6.0),
    ("Kai M.",    "Busser",    2, "5:00pm", "10:30pm", 5.5),
    ("Jalen T.",  "Busser",    2, "11:00am", "4:00pm", 5.0),
    ("Victor S.", "Dishwasher", 3, "3:00pm", "11:00pm", 8.0),
    ("Paulo C.",  "Dishwasher", 3, "10:00am", "4:00pm", 6.0),
    ("Dee W.",    "Dishwasher", 2, "5:00pm", "12:00am", 7.0),
]

# Who is trusted to lock up. Deliberately not the highest scores: being
# trusted with keys and cash is a different thing from being good on a
# Saturday, which is the whole reason the capability layer stores it as a
# flag rather than a point on the rating scale.
_EJS_CLOSERS = ("Marcus R.", "Angela M.", "Vince L.", "Dana S.", "Luis G.", "Parker S.", "Jade F.", "Hector L.")

_EJS_HOURS_V1 = (
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
# The 55-person roster adds managers and dish (9/26/26).
_EJS_HOURS = _EJS_HOURS_V1 + (
    "\n\nMANAGERS AND DISH:\n"
    "- Managers: one on every open hour; the night manager closes.\n"
    "- Dishwashers: one from 10:00am, one from 3:00pm to close; two at night Thu-Sat.\n"
    "- Nobody past 40 hours in a week: spread the hours across the role."
)

_EJS_SCHED_NOTES = (
    "Thursday through Saturday nights are the week. Sunday is a steady "
    "all-day trade rather than a rush. Monday and Tuesday are the quiet "
    "pair and are where somebody new should be learning."
)

_EJS_ROLE_RATES_V1 = {
    "Bartender": 9.00, "Server": 9.00, "Busser": 9.00, "Host": 15.00,
    "Line Cook": 21.00, "Prep Cook": 19.00,
}
_EJS_ROLE_RATES = dict(_EJS_ROLE_RATES_V1, **{"Manager": 24.00, "Dishwasher": 15.00})

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

    Never touches a restaurant carrying real uploaded shifts or one no
    longer flagged demo, and never writes settings over an admin edit: a
    redeploy must not silently undo work done in the admin panel.
    """
    from datetime import date, timedelta
    # Erik's LIVE account carries the same name (9/25/26), so the demo is
    # found by name AND the demo flag, oldest first; a live restaurant with
    # this name is never selected, seeded or logged into. With no demo row
    # left, a live one of the same name means "don't make another".
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, is_demo FROM restaurants WHERE name=? AND COALESCE(is_demo,0)=1 "
                           "ORDER BY id LIMIT 1", (SIMPLE_EJS_NAME,)).fetchone()
        live = conn.execute("SELECT id FROM restaurants WHERE name=? AND COALESCE(is_demo,0)=0 "
                            "ORDER BY id LIMIT 1", (SIMPLE_EJS_NAME,)).fetchone()
    finally:
        conn.close()

    if row:
        rid = row["id"]
    elif live:
        print(f"[auto-seed] {SIMPLE_EJS_NAME} (id={live['id']}) is a live account and there is no demo — "
              "leaving it alone")
        return None
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
    # Hours are the 55-person roster's (_ejs_history_rows' weekday totals),
    # at its blended rate.
    BY_WEEKDAY = {0: (4100, 127), 1: (3900, 116), 2: (5200, 147), 3: (7400, 189),
                  4: (11800, 228), 5: (13200, 226), 6: (8600, 146)}
    RATE = 13.96
    # The first seed's (sales, hours) pairs: a row still holding one is this
    # seed's placeholder, never a POS day, and is brought up to the roster.
    OLD = {(4100.0, 52.0), (3900.0, 50.0), (5200.0, 61.0), (7400.0, 78.0),
           (11800.0, 116.0), (13200.0, 128.0), (8600.0, 90.0)}
    conn = get_conn(db_path)
    try:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        def put(d, replace):
            sales, hours = BY_WEEKDAY[d.weekday()]
            cost = round(hours * RATE, 2)
            ds = d.strftime("%Y-%m-%d")
            if not replace:
                cur = conn.execute("SELECT sales, total_hours FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                                   (rid, ds)).fetchone()
                if cur and (float(cur[0] or 0), float(cur[1] or 0)) not in OLD:
                    return   # a real day (a POS sync) is never overwritten by a placeholder
            conn.execute("""INSERT OR REPLACE INTO labor_daily_history
                (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales,
                 total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (rid, ds, days[d.weekday()], round(cost / sales * 100, 2), cost, float(sales), float(hours)))

        d = date(2025, 6, 2)
        while d <= date(2025, 6, 29):
            put(d, True)
            d += timedelta(days=1)
        # And the trailing six weeks, anchored to today: food cost % divides
        # purchases by the sales of the same window, and a window that ends
        # today needs sales through today.
        d = date.today() - timedelta(days=41)
        while d <= date.today():
            put(d, False)
            d += timedelta(days=1)
        conn.commit()
    finally:
        conn.close()


def _seed_ejs_settings(rid: int, db_path: str):
    """Hours, rates, close times and targets — only on a restaurant that has
    never been configured, so an admin edit is never overwritten."""
    existing = get_restaurant(rid, db_path)
    if existing and (existing.hours_notes or "").strip():
        # Still exactly the first seed's text (nobody edited it): bring it
        # up to the 55-person roster — the managers and dish lines and
        # their pay rates. An edited account is left alone.
        if (existing.hours_notes or "").strip() == _EJS_HOURS_V1.strip():
            upd = {"hours_notes": _EJS_HOURS}
            try:
                if json.loads(existing.role_rates_json or "{}") == _EJS_ROLE_RATES_V1:
                    upd["role_rates_json"] = json.dumps(_EJS_ROLE_RATES)
            except (TypeError, ValueError):
                pass
            update_restaurant(rid, upd, db_path=db_path)
            print(f"[auto-seed] {SIMPLE_EJS_NAME} settings upgraded to the 55-person roster")
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
    }, db_path=db_path)
    print(f"[auto-seed] {SIMPLE_EJS_NAME} settings written")


# Sales and the share of each role on, by weekday: Monday quiet through
# Saturday peak, Sunday a steady all-day trade.
_EJS_BY_WEEKDAY = {0: (4100, 0.55), 1: (3900, 0.55), 2: (5200, 0.7), 3: (7400, 0.85),
                   4: (11800, 1.0), 5: (13200, 1.0), 6: (8600, 0.8)}
_EJS_PEAK_SHARE = 0.6          # of a role on at the Friday/Saturday peak
_EJS_ROLE_MIN = {"Manager": 1, "Server": 2, "Line Cook": 2, "Bartender": 1, "Host": 1,
                 "Busser": 1, "Prep Cook": 1, "Dishwasher": 1}
_EJS_WEEK_CAP = 40.0


def _ejs_history_rows(start, n_days):
    """[date, day, employee, role, start, end, hours, actual, sales] rows.

    Each day a role puts on its share of people for that weekday's volume,
    choosing whoever has worked the fewest days this week (full-timers first,
    part-timers filling the busy days), and nobody past five days (three
    part-time) or 40 hours: the history reads like a manager who avoids
    overtime, so the drafts it teaches do too. Deterministic."""
    from datetime import timedelta
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_role = {}
    for name, role, _score, st, en, hrs in _EJS_ROSTER:
        by_role.setdefault(role, []).append((name, st, en, float(hrs)))
    out, week_days, week_hours = [], {}, {}
    d = start
    for k in range(n_days):
        if k == 0 or d.weekday() == 0:
            week_days, week_hours = {}, {}
        sales, share = _EJS_BY_WEEKDAY[d.weekday()]
        for role, people in by_role.items():
            need = max(_EJS_ROLE_MIN.get(role, 1), int(round(len(people) * share * _EJS_PEAK_SHARE)))
            if role == "Manager" and share >= 0.85:
                need = len(people)

            def rank(p, _k=k):
                name, _st, _en, hrs = p
                pt = hrs < 7
                return (week_days.get(name, 0), 1 if (pt and share < 0.85) else 0,
                        (people.index(p) + _k) % len(people))
            picked = 0
            for name, st, en, hrs in sorted(people, key=rank):
                if picked >= need:
                    break
                max_days = 5 if hrs >= 7 else 3
                if week_days.get(name, 0) >= max_days or week_hours.get(name, 0.0) + hrs > _EJS_WEEK_CAP:
                    continue
                out.append([d.strftime("%Y-%m-%d"), days[d.weekday()], name, role, st, en, hrs, hrs, sales])
                week_days[name] = week_days.get(name, 0) + 1
                week_hours[name] = week_hours.get(name, 0.0) + hrs
                picked += 1
        d += timedelta(days=1)
    return out


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

    # The fourteen days ending yesterday, re-dated on every boot, so the
    # demo's Labor tab reads as current rather than ageing into "Out of
    # date" the way a fixed 8/31-9/13 window did (9/25/26).
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,"
             "actual_hours,sales,notes"]
    for r in _ejs_history_rows(date.today() - timedelta(days=14), 14):
        lines.append(",".join(str(x) for x in r) + ",")
    save_client_data(rid, "shifts", "\n".join(lines), source="seed", db_path=db_path)
    print(f"[auto-seed] {SIMPLE_EJS_NAME} shift data written ({len(lines) - 1} rows)")


def _seed_ejs_capabilities(rid: int, db_path: str):
    """Operational Scores and closer flags, so the Shift Quality engine has
    something to show rather than sitting dormant behind a demo."""
    existing = get_capabilities(rid, db_path=db_path) or {}
    # Per name: a person rated by hand keeps their rating; somebody the
    # roster gained since the last seed is rated from it.
    try:
        rated = {str(k).strip().lower() for k in (existing.keys() if isinstance(existing, dict) else
                                                   [e.get("employee_name") or e.get("name") for e in existing])}
    except Exception:
        rated = set()
    added = 0
    for name, _role, score, _s, _e, _h in _EJS_ROSTER:
        if name.lower() in rated:
            continue
        try:
            set_capability(rid, name, score=score, updated_by="seed", db_path=db_path)
            added += 1
        except Exception:
            pass
    has_close = {str(k).strip().lower() for k, v in (existing.items() if isinstance(existing, dict) else [])
                 if isinstance(v, dict) and "can_close" in v}   # set either way by hand: left alone
    closers = 0
    for name in _EJS_CLOSERS:
        if name.lower() in has_close:
            continue
        closers += 1
        try:
            set_capability(rid, name, attribute="can_close", flag=True,
                           updated_by="seed", db_path=db_path)
        except Exception:
            pass
    if added or closers:
        print(f"[auto-seed] {SIMPLE_EJS_NAME} ratings written for {added} staff, {closers} closer(s) flagged")


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


# "erikdemo", not "erik" (9/25/26): Erik may want "erik" for his LIVE
# account. The old demo login is renamed in place by _ensure_ejs_login.
SIMPLE_EJS_USERNAME = "erikdemo"
_LEGACY_EJS_USERNAMES = ("erik",)


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
    restaurant = get_restaurant(rid, db_path)
    conn = get_conn(db_path)
    try:
        # Rename the demo's old login ("erik" -> "erikdemo") so the live
        # account can take "erik". Only on a demo restaurant, only the login
        # this seed made, and only while the new name is free.
        if restaurant and restaurant.is_demo:
            for old in _LEGACY_EJS_USERNAMES:
                cur = conn.execute(
                    "UPDATE users SET username=? WHERE restaurant_id=? AND username=? "
                    "AND NOT EXISTS (SELECT 1 FROM users WHERE username=?)",
                    (SIMPLE_EJS_USERNAME, rid, old, SIMPLE_EJS_USERNAME))
                if cur.rowcount:
                    conn.commit()
                    print(f"[auto-seed] {SIMPLE_EJS_NAME} demo login renamed {old!r} -> {SIMPLE_EJS_USERNAME!r}")
        row = conn.execute(
            "SELECT id, username FROM users WHERE restaurant_id=? AND username=? LIMIT 1",
            (rid, SIMPLE_EJS_USERNAME)).fetchone()
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
        update_restaurant(rid, {"temp_password": wanted}, db_path=db_path)
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login reset to DEMO_PASSWORD "
              f"(username {SIMPLE_EJS_USERNAME!r})")
        return

    password = wanted or secrets.token_urlsafe(9)
    try:
        create_user(rid, SIMPLE_EJS_USERNAME, "erik+demo@cavnar.ai", password, db_path=db_path)
        update_restaurant(rid, {"temp_password": password}, db_path=db_path)
        source = "DEMO_PASSWORD" if wanted else f"generated: {password}"
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login created — username "
              f"{SIMPLE_EJS_USERNAME!r}, password from {source} "
              "(also on the admin client card)")
    except Exception as e:
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login not created: {e}")


def _seed_background():
    """Wait for the database, then seed the demo account."""
    import time as _t
    for _attempt in range(10):
        try:
            _test = get_conn()
            _test.execute("SELECT 1").fetchone()
            _test.close()
            break
        except Exception:
            _t.sleep(1)
    try:
        _auto_seed_demo_clients()
    except Exception as e:
        print(f"  demo auto-seed error: {e}")


def start_background_seed():
    """Seed the demo account off the request path. Called once from
    hosted_dashboard at boot; the thread waits for the database."""
    threading.Thread(target=_seed_background, daemon=True).start()
