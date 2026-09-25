"""Level 1 → 3: staffing starting points for a restaurant with no history.

A new account has no schedule of its own to learn a headcount from, so its
first draft was written against nothing but the owner's floors. This module
lends it one from comparable restaurants — within the privacy floor:

* Level 1, nightly (features.compute): each restaurant's own people on the
  floor per role family and daypart, per $1k of that day's sales — a
  ratio, stored in its `intel_features` row as `staff_per_1k.<family>.<daypart>`.
  No names, no dollars.
* Level 3, nightly (benchmarks.compute): the peer partition's band of each
  ratio (service model × bar-led, and — once the restaurant's own sales
  band is measured — the same sales band).
* On request (`starting_headcount`): for a restaurant with no history of its
  own, the PUBLISHED median ratio (benchmarks.published: at least
  MIN_QUARTILE_N others from privacy.MIN_ORGS organisations, the viewer's
  organisation out, coarse 0.05 step — Benchmarking audit #10, BM1-7,
  BM4-3) × this restaurant's OWN sales per weekday → people per role and
  daypart, in the restaurant's own role names, labelled borrowed. Below the
  floor, with no confirmed profile, or with no sales of its own to scale by:
  nothing, and the reason.

The ratio itself never leaves the server: `payload()` ships the rounded
headcount, the group's label and n — never `people_per_1k` — because even a
coarse ratio times the owner's own sales is a peer's staffing intensity.
Every cross-restaurant figure passes privacy.assert_anonymous before it
leaves this module.
"""
import math
import re
from datetime import date, datetime, timedelta

from models import DB_PATH
from . import privacy, categories

FEATURE_PREFIX = "staff_per_1k."
FEATURE_WINDOW_DAYS = 56
FEATURE_MIN_DAYS = 7              # days with sales and shifts before a ratio is stored
COLD_START_DAYS = 14              # shift history under this span, and nothing published: no history of its own
SALES_WEEKS = 8
DAYPARTS = ("morning", "night")

# Role words → one family, checked in order (first hit wins). Families are
# what crosses restaurants; each restaurant keeps its own role names.
ROLE_FAMILIES = (
    ("manager", r"manager|supervisor|shift lead|\bmod\b|\bgm\b|\bagm\b|sous"),
    ("barback", r"bar ?back"),
    ("bartender", r"bartend|bar ?tender|mixolog|\bbar\b"),
    ("barista", r"barista"),
    ("host", r"host"),
    ("busser", r"\bbus|busser|busboy"),
    ("runner", r"runner|expo"),
    ("server", r"server|waiter|waitress|wait ?staff"),
    ("cashier", r"cashier|counter|register"),
    ("prep_cook", r"\bprep"),
    ("dish", r"dish|porter|steward"),
    ("line_cook", r"cook|line|grill|saut|fry|pizza|chef|kitchen|oven|pantry"),
)

FAMILY_LABELS = {"manager": "managers", "barback": "barbacks", "bartender": "bartenders", "barista": "baristas",
                 "host": "hosts", "busser": "bussers", "runner": "runners", "server": "servers",
                 "cashier": "cashiers", "prep_cook": "prep cooks", "dish": "dishwashers", "line_cook": "cooks"}


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports);
    the module's own DB_PATH default means "whatever models uses now"."""
    import models as _m
    if db_path is None or db_path == DB_PATH:
        return _m.get_conn()
    return _m.get_conn(db_path)


def family_of(role: str):
    r = (role or "").strip().lower()
    if not r:
        return None
    for fam, rx in ROLE_FAMILIES:
        if re.search(rx, r):
            return fam
    return None


def metric(family: str, part: str) -> str:
    return f"{FEATURE_PREFIX}{family}.{part}"


# ── Level 1: this restaurant's own ratios ─────────────────────────────────

def compute_ratios(restaurant_id: int, today: date = None, db_path: str = DB_PATH, shifts: list = None) -> dict:
    """{staff_per_1k.<family>.<daypart>: people per $1k of the day's sales},
    averaged over the days in the window with both sales and shifts on
    file. A day a family did not work counts as 0 for it; a family never
    seen is absent, never 0. {} under FEATURE_MIN_DAYS."""
    from shift_quality import present_dayparts
    today = today or date.today()
    start = (today - timedelta(days=FEATURE_WINDOW_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        sales = {str(r["date"])[:10]: float(r["sales"]) for r in conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND sales > 0 AND date >= ? AND date <= ?",
            (restaurant_id, start, today.isoformat())).fetchall()}
    finally:
        conn.close()
    if not sales:
        return {}
    if shifts is None:
        from labor import load_shifts_for_restaurant
        shifts = load_shifts_for_restaurant(restaurant_id) or []
    people = {}        # date -> {(family, part): set(names)}
    for s in shifts:
        d = (s.get("date") or "").strip()[:10]
        name = (s.get("employee") or "").strip().lower()
        fam = family_of(s.get("role"))
        if d not in sales or not name or not fam:
            continue
        slot = people.setdefault(d, {})
        for part in present_dayparts(s):
            if part in DAYPARTS:
                slot.setdefault((fam, part), set()).add(name)
    days = [d for d in people if d in sales]
    if len(days) < FEATURE_MIN_DAYS:
        return {}
    seen = {k for d in days for k in people[d]}
    out = {}
    for fam, part in sorted(seen):
        vals = [len(people[d].get((fam, part), ())) / (sales[d] / 1000.0) for d in days]
        out[metric(fam, part)] = round(sum(vals) / len(vals), 3)
    return out


# ── the cold start ────────────────────────────────────────────────────────

def has_own_history(restaurant_id: int, shifts: list = None, db_path: str = DB_PATH) -> bool:
    """A published week of its own, or a shift history spanning at least
    COLD_START_DAYS: then its own typical headcount is the starting point."""
    conn = get_conn(db_path)
    try:
        pub = conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL LIMIT 1",
                           (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if pub:
        return True
    if shifts is None:
        from labor import load_shifts_for_restaurant
        shifts = load_shifts_for_restaurant(restaurant_id) or []
    ds = sorted({(s.get("date") or "")[:10] for s in shifts if len((s.get("date") or "")[:10]) == 10})
    if len(ds) < 2:
        return False
    try:
        span = (datetime.strptime(ds[-1], "%Y-%m-%d") - datetime.strptime(ds[0], "%Y-%m-%d")).days + 1
    except ValueError:
        return False
    return span >= COLD_START_DAYS


def _own_sales_by_weekday(restaurant_id: int, restaurant=None, db_path: str = DB_PATH):
    """({weekday: average sales}, basis) from this restaurant's own daily
    sales; failing that its own monthly revenue target spread evenly; else
    ({}, None)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND sales > 0 "
                            "AND date >= date('now', ?)", (restaurant_id, f"-{SALES_WEEKS * 7} days")).fetchall()
    finally:
        conn.close()
    acc = {}
    for r in rows:
        try:
            wd = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        acc.setdefault(wd, []).append(float(r["sales"]))
    if acc:
        return ({wd: sum(v) / len(v) for wd, v in acc.items()},
                f"your own sales by weekday over the last {SALES_WEEKS} weeks")
    target = float(getattr(restaurant, "monthly_revenue_target", 0) or 0) if restaurant is not None else 0.0
    if target > 0:
        per_day = target / 30.4
        return ({wd: per_day for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")},
                "your monthly revenue target spread evenly across the week")
    return {}, None


def starting_headcount(restaurant_id: int, restaurant=None, roster_roles: dict = None, shifts: list = None,
                       db_path: str = DB_PATH) -> dict:
    """Borrowed people per role and daypart for a restaurant with no history
    of its own. {available: True, borrowed: True, cohort, cohort_label, n,
    ratios, by_slot, headcount, basis, note} or {available: False, reason}.

    roster_roles: {name: role} — the roles to plan, in this restaurant's own
    spelling (the most common spelling of each family wins)."""
    if restaurant is None:
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else get_restaurant(restaurant_id, db_path)
    if has_own_history(restaurant_id, shifts=shifts, db_path=db_path):
        return {"available": False, "own_history": True,
                "reason": "This restaurant staffs from its own history — nothing is borrowed."}
    prof = categories.profile_for(restaurant) if restaurant is not None else None
    if not prof or not prof.get("confirmed"):
        return {"available": False, "own_history": False,
                "reason": "No restaurant profile is confirmed yet, so there is no group of similar restaurants to "
                          "borrow a starting headcount from. Confirm it under Account → Restaurant profile and the "
                          "first draft can start from theirs."}
    # Once this restaurant's own sales band is measured, borrow only from the
    # same band (#10): a $2k/day café scaled from $15k/day rooms rounds every
    # crew to 0 or 1.
    vb = None
    try:
        from . import features as _features
        own = _features.latest(restaurant_id, db_path=db_path)
        vb = ((own or {}).get("features") or {}).get("volume_band")
    except Exception:
        vb = None
    cohort = categories.partition_key(prof, "staff", volume_band=vb)
    from .benchmarks import published, cohort_label, viewer_org, MIN_QUARTILE_N
    label = cohort_label(cohort)
    roles = {}
    for _n, role in (roster_roles or {}).items():
        fam = family_of(role)
        if fam:
            roles.setdefault(fam, {}).setdefault((role or "").strip(), 0)
            roles[fam][(role or "").strip()] += 1
    if not roles:
        return {"available": False, "own_history": False,
                "reason": "The roster has no roles yet, so there is nothing to borrow a headcount for."}
    org = viewer_org(restaurant_id, db_path=db_path)
    ratios = []
    for fam in sorted(roles):
        for part in DAYPARTS:
            b = published(cohort, metric(fam, part), exclude_org=org, db_path=db_path)
            if b and not b.get("withheld") and b.get("p50") is not None:
                ratios.append({"role_family": fam, "daypart": part, "people_per_1k": float(b["p50"]),
                               "n": int(b["n"]), "week": b.get("week")})
    if not ratios:
        return {"available": False, "own_history": False, "cohort_label": label,
                "reason": (f"Fewer than {MIN_QUARTILE_N} similar restaurants ({label.lower().replace(' on cavnar', '')}) "
                           f"from at least {privacy.MIN_ORGS} separate owners have their staffing measured yet, so no "
                           f"starting headcount is borrowed — the first draft works from your floors alone.")}
    cohort_part = privacy.assert_anonymous({"cohort": cohort, "cohort_label": label, "inferred": False,
                                            "n": min(r["n"] for r in ratios), "ratios": ratios})
    sales, basis = _own_sales_by_weekday(restaurant_id, restaurant, db_path)
    if not sales:
        return {"available": False, "own_history": False, "cohort_label": label,
                "reason": ("Similar restaurants' staffing is on file, but it is measured per $1k of sales and this "
                           "restaurant has no sales of its own on file yet to scale it by — connect the POS or set a "
                           "monthly revenue target.")}
    spelled = {fam: max(names.items(), key=lambda kv: (kv[1], kv[0]))[0] for fam, names in roles.items()}
    headcount, by_slot = {}, []
    for r in ratios:
        role = spelled[r["role_family"]]
        for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            s = sales.get(wd)
            if not s:
                continue
            people = int(math.floor(r["people_per_1k"] * s / 1000.0 + 0.5))
            if people <= 0:
                continue
            headcount.setdefault((wd, r["daypart"]), {})[role] = people
            by_slot.append({"day": wd, "daypart": r["daypart"], "role": role, "people": people})
    if not headcount:
        return {"available": False, "own_history": False, "cohort_label": label,
                "reason": "Similar restaurants' ratios, scaled to your sales, come to under one person per shift — nothing borrowed."}
    return {"available": True, "borrowed": True, **cohort_part, "headcount": headcount, "by_slot": by_slot,
            "basis": basis,
            "note": (f"Borrowed: the median of {cohort_part['n']}+ {label[0].lower() + label[1:]} — people on the "
                     f"floor per $1k of sales, scaled to {basis}"
                     + ". A starting point until your own weeks replace it.")}


def merge_into_typical(typical: dict, borrowed: dict):
    """(merged, marks): the restaurant's own typical headcount with borrowed
    figures only where its own has nothing for that shift and role. marks
    is {(weekday, daypart): {lower role}} — the figures that are borrowed."""
    merged = {k: dict(v) for k, v in (typical or {}).items()}
    marks = {}
    for key, roles in (borrowed or {}).items():
        slot = merged.setdefault(tuple(key), {})
        have = {str(r).strip().lower() for r, n in slot.items() if n}
        for role, n in (roles or {}).items():
            if str(role).strip().lower() in have or not n:
                continue
            slot[role] = int(n)
            marks.setdefault(tuple(key), set()).add(str(role).strip().lower())
        if not slot:
            merged.pop(tuple(key), None)
    return merged, marks


def prompt_block(start: dict) -> str:
    if not start or not start.get("available"):
        return ""
    return ("\n\nBORROWED STARTING HEADCOUNT — this restaurant has no schedule history of its own yet. The figures "
            "marked \"(borrowed)\" in SHIFT REQUIREMENTS come from " + start["note"][len("Borrowed: "):].rstrip(".")
            + ". Treat them as a sensible first guess, below the owner's floors and anything the owner has said.")


# What never reaches a screen: the ratios themselves (#10). The rounded
# headcount, the group's label and n are what a client is shown.
_SERVER_ONLY = ("headcount", "ratios", "cohort")


def payload(start: dict) -> dict:
    """The JSON shape for the screens: headcount keyed 'Weekday|daypart',
    the group's label and n — never `people_per_1k`."""
    if not start:
        return {"available": False}
    out = {k: v for k, v in start.items() if k not in _SERVER_ONLY}
    if start.get("headcount"):
        out["headcount"] = {f"{d}|{p}": roles for (d, p), roles in start["headcount"].items()}
    return out
