"""
staff_productivity.py — each person's measured effect on the work they do,
with partial pooling and an interval, for the owner only (scheduler audit
10/10/26 LEARNING-26).

Operational strength and leadership carry about 33 points of the schedule
score and rested on owner ratings that never decay and were never checked
against anything. The one measured figure — servers' sales per guest
against the house, fixed bands, no shrinkage, no control for the section,
party size or weekday — became a rating only when the owner confirmed it.

A mixed model per measure, on the ticket archive (pos_tickets):

  sales_per_guest   net / guests of each ticket a person rang, as a share
                    above or below the mean of the same slot — business
                    date x meal x party size (1, 2, 3-4, 5+) — so a big
                    party, a Friday or a brunch is never read as the person;
  turn_minutes      a dine-in table's open-to-close minutes, the same slots
                    (lower is faster);
  tickets_per_hour  the tickets a person rang per hour they worked, against
                    the same date x meal's other ringers.

The person effect is the mean residual, shrunk toward 0 (the house) by the
empirical-Bayes weight n·tau² / (n·tau² + sigma²) — tau² the spread between
people, sigma² within (method of moments) — with its posterior interval.
A figure is said only past MIN_TICKETS, and is "measured" only when its 90%
interval excludes the house.

Personnel data: the owner's alone (schedule_memory.suggested_ratings, the
principal-only route) — never in a prompt, never on a staff surface. A
rating DISAGREES with the measure when it is 4-5 with the person measured
below the house, or 1-2 measured above (interval clear of 0); a rating is
STALE past RATING_STALE_DAYS. A measured effect is a suggested rating for
someone unrated — the owner confirms it before it counts.
"""
import json
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

WINDOW_DAYS = 56
MIN_TICKETS = 20
Z90 = 1.645
RATING_STALE_DAYS = 180
MIN_PEOPLE = 3                    # people measured before a between-person spread is read
MEASURES = ("sales_per_guest", "turn_minutes", "tickets_per_hour")


def get_conn(db_path=None):
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_staff_productivity(db_path=None):
    """Boot DDL (models.init_db): the weekly per-person estimates."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_productivity (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            person_key      TEXT    NOT NULL,
            name            TEXT    NOT NULL,
            estimate_json   TEXT    NOT NULL,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, person_key)
        )""")
        conn.commit()
    finally:
        conn.close()


def _party(g) -> str:
    g = int(g or 0)
    return "1" if g <= 1 else "2" if g == 2 else "3-4" if g <= 4 else "5+"


def shrunk_effects(obs: dict) -> dict:
    """{person: {"est", "lo", "hi", "n", "raw"}} from {person: [residual]} —
    the empirical-Bayes partial pooling of the module note. Pure."""
    people = {p: v for p, v in (obs or {}).items() if len(v) >= 2}
    if len(people) < MIN_PEOPLE:
        return {}
    within = []
    for v in people.values():
        m = sum(v) / len(v)
        within += [(x - m) ** 2 for x in v]
    dof = sum(len(v) - 1 for v in people.values())
    sigma2 = (sum(within) / dof) if dof > 0 else 0.0
    means = {p: sum(v) / len(v) for p, v in people.items()}
    grand = sum(means.values()) / len(means)
    between = sum((m - grand) ** 2 for m in means.values()) / max(1, len(means) - 1)
    tau2 = max(1e-6, between - sum(sigma2 / len(v) for v in people.values()) / len(people))
    out = {}
    for p, v in people.items():
        n = len(v)
        w = n * tau2 / (n * tau2 + sigma2) if sigma2 > 0 else 1.0
        est = w * means[p]
        var = (tau2 * sigma2) / (n * tau2 + sigma2) if sigma2 > 0 else 0.0
        half = Z90 * math.sqrt(var)
        out[p] = {"est": round(est, 4), "lo": round(est - half, 4), "hi": round(est + half, 4), "n": n,
                  "raw": round(means[p], 4), "weight": round(w, 3)}
    return out


def _residuals(tickets, key_of, value_of, slot_of, relative=False) -> dict:
    """{person: [value - its slot's mean]} over the tickets with a value —
    with `relative`, (value / the slot's mean) - 1: a share above or below
    the house in the same slot."""
    slots = {}
    pts = []
    for t in tickets:
        who, v = key_of(t), value_of(t)
        if not who or v is None:
            continue
        s = slot_of(t)
        slots.setdefault(s, []).append(v)
        pts.append((who, s, v))
    mean = {s: sum(v) / len(v) for s, v in slots.items()}
    out = {}
    for who, s, v in pts:
        if len(slots[s]) >= 2:
            if relative:
                if mean[s] > 0:
                    out.setdefault(who, []).append(v / mean[s] - 1.0)
            else:
                out.setdefault(who, []).append(v - mean[s])
    return out


def estimate_from(tickets: list, hours: dict = None) -> dict:
    """{person key: {"name", "measures": {measure: effect}}} from ticket dicts
    (server_name, guest_count, net_sales, business_date, mealtime,
    opened_at, closed_at, room_name, is_bar) and {(person key, date):
    hours worked}. Pure — the tests' entry."""
    import service_performance as _sp
    from staff_settings import name_key
    names = {}

    def who(t):
        n = (t.get("server_name") or "").strip()
        if not n:
            return None
        k = name_key(n)
        names.setdefault(k, n)
        return k

    def slot(t):
        return (str(t.get("business_date"))[:10], t.get("mealtime") or "", _party(t.get("guest_count")))
    spg = _residuals([t for t in tickets if (t.get("guest_count") or 0) > 0 and (t.get("net_sales") or 0) > 0],
                     who, lambda t: float(t["net_sales"]) / float(t["guest_count"]), slot, relative=True)
    turn = _residuals(tickets, who, lambda t: _sp._turn(t), slot)
    tph = {}
    if hours:
        rung = {}
        for t in tickets:
            k = who(t)
            if k:
                key = (k, str(t.get("business_date"))[:10])
                rung[key] = rung.get(key, 0) + 1
        per_day = {}
        for (k, d), n in rung.items():
            h = hours.get((k, d))
            if h and h > 0:
                per_day.setdefault(d, []).append((k, n / float(h)))
        for d, vals in per_day.items():
            if len(vals) < 2:
                continue
            m = sum(v for _k, v in vals) / len(vals)
            for k, v in vals:
                tph.setdefault(k, []).append(v - m)
    out = {}
    for measure, obs in (("sales_per_guest", spg), ("turn_minutes", turn), ("tickets_per_hour", tph)):
        n_by = {p: len(v) for p, v in obs.items()}
        for p, e in shrunk_effects(obs).items():
            if measure != "tickets_per_hour" and n_by.get(p, 0) < MIN_TICKETS:
                continue
            e["measured"] = e["lo"] > 0 or e["hi"] < 0
            out.setdefault(p, {"name": names.get(p, p), "measures": {}})["measures"][measure] = e
    return out


def judge_rating(measures: dict, rating, updated_at=None, today=None) -> dict:
    """{"disagrees", "stale", "why"} — a 4-5 rating with the person measured
    below the house on sales per guest (or slower turns), or a 1-2 measured
    above it, interval clear of 0; stale past RATING_STALE_DAYS. Pure."""
    out = {"disagrees": False, "stale": False, "why": None}
    try:
        if updated_at:
            age = ((today or date.today()) - date.fromisoformat(str(updated_at)[:10])).days
            out["stale"] = age > RATING_STALE_DAYS
            out["age_days"] = age
    except ValueError:
        pass
    if rating is None:
        return out
    s = (measures or {}).get("sales_per_guest") or {}
    t = (measures or {}).get("turn_minutes") or {}
    below = (s.get("measured") and s["hi"] < 0) or (t.get("measured") and t["lo"] > 0)
    above = (s.get("measured") and s["lo"] > 0) or (t.get("measured") and t["hi"] < 0)
    if int(rating) >= 4 and below and not above:
        out.update(disagrees=True, why="rated highly, measured below the house")
    elif int(rating) <= 2 and above and not below:
        out.update(disagrees=True, why="rated low, measured above the house")
    return out


def estimate_restaurant(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """Estimate everyone who rang tickets in the window and store it
    (staff_productivity). Personnel data — the owner's alone."""
    import service_performance as _sp
    import shift_facts
    from staff_settings import name_key
    today = today or date.today()
    start, end = _sp._window(WINDOW_DAYS, today)
    tickets, _comps, _days = _sp._tickets(restaurant_id, start, end, db_path)
    try:
        import people
        canon = people.canonical_names(restaurant_id, sorted({t["server_name"] for t in tickets if t.get("server_name")}),
                                       db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        canon = {}
    for t in tickets:
        if t.get("server_name"):
            t["server_name"] = canon.get(t["server_name"], t["server_name"])
    hours = {}
    for r in shift_facts.rows(restaurant_id, since=start.isoformat(), until=end.isoformat(),
                              db_path=None if db_path == DB_PATH else db_path):
        try:
            h = float(r.get("actual_hours") or 0)
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            key = (name_key(r.get("employee")), str(r.get("date"))[:10])
            hours[key] = hours.get(key, 0.0) + h
    got = estimate_from(tickets, hours)
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM staff_productivity WHERE restaurant_id=?", (restaurant_id,))
        for k, e in got.items():
            conn.execute("INSERT INTO staff_productivity (restaurant_id, person_key, name, estimate_json) "
                         "VALUES (?,?,?,?)", (restaurant_id, k, e["name"],
                                              json.dumps(dict(e, window=[start.isoformat(), end.isoformat()]))))
        conn.commit()
    finally:
        conn.close()
    return got


def stored(restaurant_id, db_path=DB_PATH) -> dict:
    """{person key: estimate} as last stored; {} on any failure."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT person_key, estimate_json FROM staff_productivity WHERE restaurant_id=?",
                                (restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[r["person_key"]] = json.loads(r["estimate_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
    return out
