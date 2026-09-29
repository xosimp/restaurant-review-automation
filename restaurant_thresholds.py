"""restaurant_thresholds — the margins that trigger advice, fitted to each
restaurant's own noise (memory audit 9/29/26, "thresholds").

thresholds.LABOR_OVER_TARGET_PTS (3 points) decided for every restaurant
alike when labor is "over target": a restaurant whose labor % swings five
points on its own was flagged on noise, however steady its week was
underneath. What the engine measures about the restaurant's own spread
(metrics.window_spread_detail, the band outcomes and the weekly review
already read) never reached the thresholds that fire advice.

Each margin here is the LARGER of the stated margin and k·σ of this
restaurant's own history — weekly_review.week_band's rule: the stated
margin is the floor a restaurant with thin history gets, and its own
spread can only widen it, never narrow it. Stored with its provenance —
the value, the stated floor, σ, k, how many windows or days it rests on,
the date it was fitted and the algorithm version — refreshed nightly by
the learning pass (learning_memory), read on request by margin(). A
restaurant with nothing stored reads the stated margin.

  labor_over_period   a period's labor % against its target (the labor
                      alert, the labor issue, Home's attention item and
                      pulse): σ of this restaurant's 28-day labor % windows
  labor_over_day      one day's labor % against its target (the
                      overstaffed and strong-day lists behind trim_day):
                      σ of its daily labor % around each weekday's own mean
"""
import logging
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from thresholds import LABOR_OVER_TARGET_PTS

log = logging.getLogger(__name__)

THRESHOLDS_VERSION = 1
# The one-sided width of the band (metrics.BAND_K): with nothing wrong, a
# period reads past it about one time in twenty.
MARGIN_K = 1.645
DAY_HISTORY_DAYS = 84
MIN_DAYS_FOR_DAY_SIGMA = 20
PERIOD_WINDOW_DAYS = 28
# A fitted margin older than this is not trusted on its own: the stated
# margin stands until the next nightly pass refits it.
MAX_AGE_DAYS = 45

STATED = {"labor_over_period": LABOR_OVER_TARGET_PTS, "labor_over_day": LABOR_OVER_TARGET_PTS}


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_restaurant_thresholds(db_path: str = DB_PATH):
    """Boot DDL (models.init_db). One row per restaurant and margin, the
    current fit; small and kept."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS restaurant_thresholds (
            restaurant_id   INTEGER NOT NULL,
            name            TEXT    NOT NULL,
            value           REAL    NOT NULL,
            stated          REAL    NOT NULL,
            sigma           REAL,
            k               REAL,
            n               INTEGER NOT NULL DEFAULT 0,
            method          TEXT,
            basis           TEXT,
            as_of           TEXT    NOT NULL,
            version         INTEGER NOT NULL,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, name)
        )""")
        conn.commit()
    finally:
        conn.close()


def _period_fit(restaurant_id, today, db_path):
    import metrics
    kw = {"db_path": db_path} if db_path else {}
    sp = metrics.window_spread_detail(restaurant_id, "labor_pct", PERIOD_WINDOW_DAYS,
                                      (today - timedelta(days=1)).isoformat(), **kw)
    return sp.get("sigma"), int(sp.get("n") or 0), sp.get("method")


def _day_fit(restaurant_id, today, db_path):
    """σ of daily labor % around each weekday's own mean, over the last
    DAY_HISTORY_DAYS — a Friday that always runs heavier is a pattern, not
    noise, and is left in the day's mean."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT day_of_week, labor_cost, sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? "
            "AND date<? AND sales IS NOT NULL AND sales > 0 AND labor_cost IS NOT NULL",
            (restaurant_id, (today - timedelta(days=DAY_HISTORY_DAYS)).isoformat(), today.isoformat())).fetchall()
    finally:
        conn.close()
    by = {}
    for r in rows:
        by.setdefault(r["day_of_week"], []).append(float(r["labor_cost"]) / float(r["sales"]) * 100.0)
    resid = []
    for vals in by.values():
        if len(vals) < 2:
            continue
        m = sum(vals) / len(vals)
        # Bessel's correction per group: each weekday's mean spends a degree
        # of freedom.
        scale = (len(vals) / (len(vals) - 1.0)) ** 0.5
        resid += [(v - m) * scale for v in vals]
    if len(resid) < MIN_DAYS_FOR_DAY_SIGMA:
        return None, len(resid), "stated"
    sigma = (sum(x * x for x in resid) / len(resid)) ** 0.5
    return sigma, len(resid), "daily, around each weekday's mean"


def fit(restaurant_id, today=None, db_path=None) -> dict:
    """{name: row} — every margin fitted to this restaurant's history now.
    Never raises (a margin that cannot be fitted is the stated one)."""
    today = today or date.today()
    out = {}
    for name, fn, what in (("labor_over_period", _period_fit, f"{PERIOD_WINDOW_DAYS}-day labor %"),
                           ("labor_over_day", _day_fit, "daily labor %")):
        stated = STATED[name]
        try:
            sigma, n, method = fn(restaurant_id, today, db_path)
        except Exception as e:
            log.warning("restaurant_thresholds: %s not fitted for rid=%s: %s", name, restaurant_id, e)
            sigma, n, method = None, 0, "stated"
        own = round(MARGIN_K * float(sigma), 2) if sigma is not None else None
        value = round(max(stated, own), 2) if own is not None else stated
        if own is None:
            basis = (f"the stated {stated:g}-point margin — too little of this restaurant's own {what} history "
                     f"to fit one ({n} {'windows' if name == 'labor_over_period' else 'days'})")
        elif own > stated:
            basis = (f"{MARGIN_K:g}× this restaurant's own spread of {what} (σ {sigma:.1f} points over {n} "
                     f"{'windows' if name == 'labor_over_period' else 'days'}) — wider than the stated "
                     f"{stated:g} points, so a swing this size is not called over target")
        else:
            basis = (f"the stated {stated:g}-point margin — wider than {MARGIN_K:g}× this restaurant's own spread "
                     f"of {what} (σ {sigma:.1f} points over {n} {'windows' if name == 'labor_over_period' else 'days'})")
        out[name] = {"name": name, "value": value, "stated": stated,
                     "sigma": round(float(sigma), 3) if sigma is not None else None, "k": MARGIN_K, "n": n,
                     "method": method, "basis": basis, "as_of": today.isoformat(), "version": THRESHOLDS_VERSION}
    return out


def refresh(restaurant_id, today=None, db_path=None) -> int:
    """Fit and store every margin (the nightly learning pass). Returns rows
    written. Never raises."""
    rows = fit(restaurant_id, today=today, db_path=db_path)
    try:
        conn = get_conn(db_path)
    except Exception:
        return 0
    try:
        for r in rows.values():
            conn.execute(
                "INSERT INTO restaurant_thresholds (restaurant_id, name, value, stated, sigma, k, n, method, basis, "
                "as_of, version, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(restaurant_id, name) DO UPDATE SET value=excluded.value, stated=excluded.stated, "
                "sigma=excluded.sigma, k=excluded.k, n=excluded.n, method=excluded.method, basis=excluded.basis, "
                "as_of=excluded.as_of, version=excluded.version, computed_at=excluded.computed_at",
                (restaurant_id, r["name"], r["value"], r["stated"], r["sigma"], r["k"], r["n"], r["method"],
                 r["basis"], r["as_of"], r["version"]))
        conn.commit()
        return len(rows)
    except Exception as e:
        log.warning("restaurant_thresholds: not stored for rid=%s: %s", restaurant_id, e)
        return 0
    finally:
        conn.close()


def detail(restaurant_id, name, db_path=None):
    """The stored fit (dict) when it is current (MAX_AGE_DAYS) and of this
    version, else None. Never raises."""
    if not restaurant_id or name not in STATED:
        return None
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT * FROM restaurant_thresholds WHERE restaurant_id=? AND name=?",
                           (restaurant_id, name)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if row is None or int(row["version"] or 0) != THRESHOLDS_VERSION:
        return None
    try:
        if (date.today() - date.fromisoformat(str(row["as_of"])[:10])).days > MAX_AGE_DAYS:
            return None
    except ValueError:
        return None
    return dict(row)


def margin(restaurant_id, name, stated=None, db_path=None) -> float:
    """The margin to apply for `name` at this restaurant: its current fit,
    never below the stated margin (`stated`, default STATED[name]); the
    stated margin when nothing current is stored. Never raises."""
    floor = float(stated if stated is not None else STATED.get(name, LABOR_OVER_TARGET_PTS))
    d = detail(restaurant_id, name, db_path=db_path)
    if not d:
        return floor
    try:
        return max(floor, float(d["value"]))
    except (TypeError, ValueError):
        return floor
