"""pos_capacity — the POS request budget's policy (RPOWER audit #23, 10/21/26:
A4-12, A5-01, A5-03, A4-11 / A5-11, A5-13).

rpower's token buckets (rpower._take_slot — F1's primitive) decide WHETHER a
request may go now; this module decides how the budget is SHARED and how much
work is let into it:

  lane shares   by time of day on the operator's clock (OPERATOR_TZ):
                  service 07:00-22:00   live 60 %   nightly 30 %   bulk 10 %
                  close   22:00-03:00   close 50 %  nightly 40 %   bulk 10 %
                  nightly 03:00-07:00   nightly 80 % bulk 20 %
                Live and close are the priority lanes: never capped, served
                first in a process, and left whatever the background lanes'
                caps leave. A background lane (nightly, bulk) is held to its
                share only while a lane above it is being used somewhere
                (rpower's ":live" / ":nightly" marks) — BORROWING: an idle
                share flows to whoever is waiting, so at 4am with no live
                reads the archive takes the whole budget, and onboarding
                takes it all on a quiet afternoon. RPOWER_LANE_SHARES (JSON,
                {"service": {"nightly": 0.3, "bulk": 0.1}, ...}) overrides.

  backpressure  `budget_check` (the nightly `pos_budget_check` job, 2am CT):
                tonight's demand (stores x measured requests per store-night,
                from the lane ledger) against the nightly window's budget.
                Over it, bulk is SHED first (onboarding waits for the window
                to end; bulk keeps a sliver and stops borrowing), and when
                even that cannot fit the night, live labor's cadence is
                STRETCHED outside each store's decision windows (labor_live).
                Close (the DSR) and the nightly archive are never shed. The
                state is one job_cursors row (STATE_KEY) every process reads.

  utilisation   `utilisation(hours)` — requests per lane per hour (the lane
                ledger: requests per UTC hour per lane, written by
                `note_lane` on every request that took a slot, kept in
                `pos_lane_ledger`) against that
                lane's budget in that hour; the console's "budget
                utilisation %" (F6 shows it), and `budget_check` counts the
                nights the nightly lane ran above UTIL_ALERT_PCT.

  sizing        the queue's POS groups sized from the budget (Little's law:
                slots = requests a second x seconds a request x headroom),
                never a fixed 4: `pos_io_concurrency` (the nightly archive and
                sync), `live_concurrency` (live labor's rows), and the
                onboarding admission rate (`onboard_not_before`: ~35 stores an
                hour at 10 req/s, so a 1,500-store rollout is fed in, never
                enqueued at once).

  ledger rollup `roll_ledger_daily` (ops' retention rollup for
                pos_request_ledger, A5-13): hourly rows older than
                LEDGER_HOURLY_DAYS become one row per day ("YYYY-MM-DD" in
                the hour column), so the ledger grows by stores x endpoints a
                day after a week, not x 24.

Nothing here raises into a request: an unreadable state is "not shed", an
unreadable bucket count is the fleet ceiling.
"""
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("pos_capacity")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (never a bound copy)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _float_env(name, default):
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return float(default)


# ── lane shares by time of day ──────────────────────────────────────────────

LANE_TZ = os.getenv("RPOWER_LANE_TZ", "America/Chicago")
# (window, first hour, last hour exclusive) on LANE_TZ's clock; "close" wraps
# midnight.
WINDOWS = (("nightly", 3, 7), ("service", 7, 22), ("close", 22, 3))
DEFAULT_SHARES = {
    "service": {"live": 0.60, "close": 0.0, "nightly": 0.30, "bulk": 0.10},
    "close": {"live": 0.0, "close": 0.50, "nightly": 0.40, "bulk": 0.10},
    "nightly": {"live": 0.0, "close": 0.0, "nightly": 0.80, "bulk": 0.20},
}
# Bulk's share while it is shed (backpressure): a sliver, so a retry of an
# onboarding row already running still finishes, and no borrowing.
SHED_BULK_SHARE = 0.02
BACKGROUND = ("nightly", "bulk")


def _shares():
    raw = (os.getenv("RPOWER_LANE_SHARES") or "").strip()
    out = {w: dict(v) for w, v in DEFAULT_SHARES.items()}
    if not raw:
        return out
    try:
        given = json.loads(raw)
        for w, lanes in (given or {}).items():
            if w in out and isinstance(lanes, dict):
                for lane, v in lanes.items():
                    out[w][str(lane)] = min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError) as e:
        log.warning("RPOWER_LANE_SHARES unreadable (%s); the default shares stand", e)
    return out


def _lane_now(now=None):
    """`now` (aware, or naive on LANE_TZ's clock) as naive LANE_TZ time."""
    from zoneinfo import ZoneInfo
    if now is None:
        return datetime.now(ZoneInfo(LANE_TZ)).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(ZoneInfo(LANE_TZ)).replace(tzinfo=None)
    return now


def window(now=None) -> str:
    """'service', 'close' or 'nightly' for this moment on LANE_TZ's clock."""
    h = _lane_now(now).hour
    for name, a, z in WINDOWS:
        if (a <= h < z) if a < z else (h >= a or h < z):
            return name
    return "service"


def share(lane, now=None) -> float:
    """The lane's share of the budget in this moment's window; for a
    priority lane (live, close) what the background caps leave it."""
    w = _shares()[window(now)]
    if lane in BACKGROUND:
        s = float(w.get(lane, 0.0))
        if lane == "bulk" and bulk_shed():
            s = min(s, SHED_BULK_SHARE)
        return s
    return max(0.0, 1.0 - float(w.get("nightly", 0.0)) - float(w.get("bulk", 0.0)))


def background_share(lane, priority_active=False, nightly_active=False, now=None):
    """The cap a `lane` request's sub-bucket runs at (a fraction of the
    bucket's rate), or None for no cap. Live and close: None. nightly: its
    share while live or close requests are being made on the bucket, else
    None (it borrows the idle share). bulk: its share while any lane above it
    is in use, else None — except while shed, when it keeps its sliver and
    never borrows."""
    if lane not in BACKGROUND:
        return None
    s = share(lane, now)
    if lane == "bulk" and bulk_shed():
        return max(0.005, s)
    above = priority_active or (lane == "bulk" and nightly_active)
    if not above:
        return None
    return max(0.01, s)


# ── the lane ledger (utilisation) ───────────────────────────────────────────
#
# `pos_lane_ledger`: requests per UTC hour per lane, every process's counts
# added together (one upsert batch per LANE_FLUSH_SECONDS per process, like
# pos_io's request ledger, which is per endpoint and store and has no lane).

_DDL = (
    """CREATE TABLE IF NOT EXISTS pos_lane_ledger (
        hour        TEXT NOT NULL,
        lane        TEXT NOT NULL,
        n           INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hour, lane)
    )""",
)
LANE_FLUSH_SECONDS = 30.0
_lane_counts = {}
_lane_lock = threading.Lock()
_lane_state = {"flushed_at": time.monotonic()}


def init_pos_capacity(db_path=None):
    """The lane ledger (boot only, models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def note_lane(lane) -> None:
    """One request that took its slot in `lane` (rpower._sleep_for_spacing):
    counted in this process's hourly aggregate, written at most every
    LANE_FLUSH_SECONDS. Never raises."""
    try:
        key = (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00:00"), str(lane or "nightly"))
        with _lane_lock:
            _lane_counts[key] = _lane_counts.get(key, 0) + 1
            due = time.monotonic() - _lane_state["flushed_at"] >= LANE_FLUSH_SECONDS
            if due:
                _lane_state["flushed_at"] = time.monotonic()
        if due:
            flush_lanes()
    except Exception as e:
        log.debug("lane count skipped: %s", e)


def flush_lanes(db_path=None) -> int:
    """This process's lane counts into pos_lane_ledger. A failed write drops
    them (an operator's reading, never a record anything depends on)."""
    with _lane_lock:
        items = list(_lane_counts.items())
        _lane_counts.clear()
    if not items:
        return 0
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany("INSERT INTO pos_lane_ledger (hour, lane, n) VALUES (?,?,?) ON CONFLICT(hour, lane) "
                             "DO UPDATE SET n=pos_lane_ledger.n+excluded.n",
                             [(h, lane, int(n)) for (h, lane), n in items])
            conn.commit()
        finally:
            conn.close()
        return len(items)
    except Exception as e:
        log.warning("pos_lane_ledger write failed (counts dropped): %s", e)
        return 0


def reset_for_tests():
    """Forget this process's unflushed lane counts and kept readings."""
    with _lane_lock:
        _lane_counts.clear()
    _forget_state()


def _flush_at_exit():
    try:
        flush_lanes()
    except Exception as e:
        log.debug("lane counts not flushed at exit: %s", e)


import atexit as _atexit
_atexit.register(_flush_at_exit)


def _spacing():
    return max(0.0, _float_env("RPOWER_REQUEST_SPACING_SECONDS", 0.15))


def fleet_rps() -> float:
    return max(0.0, _float_env("RPOWER_FLEET_RPS", 10))


_rps_cache = {"at": 0.0, "value": None}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("pos_capacity._rps_cache", _rps_cache)
_rps_lock = threading.Lock()
RPS_CACHE_SECONDS = 600


def effective_rps(db_path=None) -> float:
    """Requests a second the platform can send to RPOWER: the fleet ceiling,
    or — when that is higher, per-token spacing is on and the budget is per
    token (RPOWER_BUDGET_PER_STORE off) — the token buckets in use times one
    token's rate (A5-01: one partner token for every store is 6.67 req/s,
    under the 10 req/s fleet cap). Read at most every RPS_CACHE_SECONDS."""
    now = time.monotonic()
    with _rps_lock:
        if _rps_cache["value"] is not None and now - _rps_cache["at"] < RPS_CACHE_SECONDS and not db_path:
            return _rps_cache["value"]
    fleet = fleet_rps()
    spacing = _spacing()
    per_store = os.getenv("RPOWER_BUDGET_PER_STORE", "0").strip().lower() in ("1", "true", "on", "yes")
    value = fleet if fleet > 0 else (1.0 / spacing if spacing else 10.0)
    if spacing > 0 and not per_store:
        try:
            conn = get_conn(db_path)
            try:
                cutoff = time.time() - 7 * 86400
                rows = conn.execute("SELECT bucket FROM rpower_rate_bucket WHERE updated_at > ? AND bucket LIKE ?",
                                    (cutoff, "rpower:%")).fetchall()
            finally:
                conn.close()
            tokens = {str(r[0]) for r in rows
                      if str(r[0]).count(":") == 1 and str(r[0]) != "rpower:fleet"}
            if tokens:
                value = min(value, len(tokens) / spacing) if fleet > 0 else len(tokens) / spacing
        except Exception:
            pass
    with _rps_lock:
        _rps_cache.update(at=now, value=value)
    return value


def _ledger_rows(hours, db_path=None):
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%d %H:00:00")
    flush_lanes(db_path)
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT hour, lane, n FROM pos_lane_ledger WHERE hour >= ?", (since,)).fetchall()]
    finally:
        conn.close()


def utilisation(hours=24, db_path=None) -> dict:
    """Budget utilisation % per lane over the last `hours` (the lane ledger):
    {rps, lanes: {lane: {requests, budget, pct, peak_hour_pct, peak_hour}},
    total: {requests, budget, pct}}. A lane's budget in an hour is the
    effective rps x 3,600 x its share in that hour's window."""
    rps = effective_rps(db_path)
    try:
        rows = _ledger_rows(hours, db_path)
    except Exception as e:
        log.warning("lane ledger unreadable: %s", e)
        rows = []
    lanes = {}
    total_n = 0
    for r in rows:
        try:
            at = datetime.strptime(str(r["hour"])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lane = str(r["lane"])
        n = int(r["n"] or 0)
        total_n += n
        budget = rps * 3600 * share(lane, at)
        d = lanes.setdefault(lane, {"requests": 0, "budget": 0.0, "peak_hour_pct": 0.0, "peak_hour": None})
        d["requests"] += n
        d["budget"] += budget
        pct = (100.0 * n / budget) if budget > 0 else None
        if pct is not None and pct > d["peak_hour_pct"]:
            d["peak_hour_pct"], d["peak_hour"] = round(pct, 1), str(r["hour"])
    for d in lanes.values():
        d["pct"] = round(100.0 * d["requests"] / d["budget"], 1) if d["budget"] > 0 else None
        d["budget"] = int(d["budget"])
    budget_total = rps * 3600 * hours
    return {"hours": hours, "rps": round(rps, 2), "lanes": lanes,
            "total": {"requests": total_n, "budget": int(budget_total),
                      "pct": round(100.0 * total_n / budget_total, 1) if budget_total else None}}


# ── backpressure ────────────────────────────────────────────────────────────

STATE_KEY = "pos_budget_state"
# What one store costs a night when the ledger has not measured it yet (A5
# §3.2: the 3-7am chain, 76 requests at Simple EJ's, 10/20/26).
NIGHTLY_REQUESTS_PER_STORE = _float_env("POS_NIGHTLY_REQUESTS_PER_STORE", 76)
# The nightly window's length the budget is judged over (hours).
NIGHTLY_WINDOW_HOURS = _float_env("POS_NIGHTLY_WINDOW_HOURS", 4)
# A night whose nightly lane peaked above this is counted; three in a row is
# said to the operator (ops.capture under the job's name, F6 surfaces it).
UTIL_ALERT_PCT = _float_env("POS_BUDGET_ALERT_PCT", 85)
UTIL_ALERT_NIGHTS = 3
_state_cache = {"at": 0.0, "value": None}
_process_caches.register("pos_capacity._state_cache", _state_cache)
STATE_CACHE_SECONDS = 60


def state(db_path=None) -> dict:
    """The last budget check's verdict ({} before the first): shed_bulk,
    stretch_live, demand, budget, ratio, at, over_nights, utilisation.
    Read at most every STATE_CACHE_SECONDS per process."""
    now = time.monotonic()
    if _state_cache["value"] is not None and now - _state_cache["at"] < STATE_CACHE_SECONDS and not db_path:
        return _state_cache["value"]
    value = {}
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (STATE_KEY,)).fetchone()
        finally:
            conn.close()
        value = json.loads(row[0]) if row and row[0] else {}
        # Bulk is shed until the nightly window ends; live labor is stretched
        # through the service day after it (the night's spill reaches it).
        if value.get("until") and float(value["until"]) < time.time():
            value = dict(value, shed_bulk=False)
        stretch_until = value.get("stretch_until") or value.get("until")
        if stretch_until and float(stretch_until) < time.time():
            value = dict(value, stretch_live=False)
    except Exception:
        value = {}
    _state_cache.update(at=now, value=value)
    return value


def _forget_state():
    _state_cache.update(at=0.0, value=None)
    _rps_cache.update(at=0.0, value=None)


def bulk_shed(db_path=None) -> bool:
    """Whether bulk work is shed now: tonight's verdict said so and it is the
    close or nightly window (a check run by hand at 10am never holds
    onboarding through the day's spare budget)."""
    if (os.getenv("POS_BUDGET_SHED", "") or "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return bool(state(db_path).get("shed_bulk")) and window() != "service"


def live_stretched(db_path=None) -> bool:
    if (os.getenv("POS_BUDGET_SHED", "") or "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return bool(state(db_path).get("stretch_live"))


def _connected_stores(db_path=None) -> int:
    """Restaurants in service with RPOWER connected (the demand's store
    count)."""
    try:
        import models
        import pos
        n = 0
        for r in models.get_all_restaurants():
            if not models.in_service(r):
                continue
            name, _mod = pos.connected_provider(r.id)
            if name == "rpower":
                n += 1
        return n
    except Exception as e:
        log.warning("budget check: stores unreadable: %s", e)
        return 0


def measured_per_store(db_path=None, stores=None):
    """Requests per store over the last 24 hours' nightly lane, or None
    before the lane ledger has a night (or with no stores)."""
    stores = stores if stores is not None else _connected_stores(db_path)
    if not stores:
        return None
    try:
        rows = _ledger_rows(24, db_path)
    except Exception:
        return None
    n = sum(int(r["n"] or 0) for r in rows if str(r["lane"]) == "nightly")
    return (n / float(stores)) if n else None


def budget_check(db_path=None, now=None) -> dict:
    """The nightly `pos_budget_check` job: tonight's demand against the
    nightly window's budget, the verdict kept for every process (STATE_KEY)
    until the window ends. Sheds bulk when the night does not fit; stretches
    live labor's cadence when it would not fit even with bulk shed. Never
    sheds close or nightly. Standard counts plus the figures."""
    import ops
    stores = _connected_stores(db_path)
    rps = effective_rps(db_path)
    per_store = measured_per_store(db_path, stores) or NIGHTLY_REQUESTS_PER_STORE
    demand = stores * per_store
    shares = _shares()["nightly"]
    nightly_budget = rps * 3600 * NIGHTLY_WINDOW_HOURS * float(shares.get("nightly", 0.8))
    whole = rps * 3600 * NIGHTLY_WINDOW_HOURS * (float(shares.get("nightly", 0.8)) + float(shares.get("bulk", 0.2)))
    shed_bulk = demand > nightly_budget
    stretch_live = demand > whole
    util = {}
    try:
        util = utilisation(24, db_path)
    except Exception as e:
        log.warning("budget check: utilisation unreadable: %s", e)
    prev = {}
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (STATE_KEY,)).fetchone()
        finally:
            conn.close()
        prev = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        prev = {}
    peak = ((util.get("lanes") or {}).get("nightly") or {}).get("peak_hour_pct") or 0.0
    over_nights = int(prev.get("over_nights") or 0) + 1 if peak > UTIL_ALERT_PCT else 0
    local = _lane_now(now)
    end = local.replace(hour=7, minute=0, second=0, microsecond=0)
    if local.hour >= 7:
        end += timedelta(days=1)
    from zoneinfo import ZoneInfo
    until = end.replace(tzinfo=ZoneInfo(LANE_TZ)).timestamp()
    stretch_until = end.replace(hour=22, tzinfo=ZoneInfo(LANE_TZ)).timestamp()
    value = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "stores": stores,
             "rps": round(rps, 2), "per_store": round(per_store, 1), "demand": int(demand),
             "budget": int(nightly_budget), "ratio": round(demand / nightly_budget, 3) if nightly_budget else None,
             "shed_bulk": bool(shed_bulk), "stretch_live": bool(stretch_live), "until": until,
             "stretch_until": stretch_until,
             "over_nights": over_nights, "nightly_peak_pct": peak,
             "lanes": {k: {"pct": v.get("pct"), "peak_hour_pct": v.get("peak_hour_pct")}
                       for k, v in (util.get("lanes") or {}).items()}}
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (STATE_KEY, json.dumps(value)))
        conn.commit()
    finally:
        conn.close()
    _forget_state()
    if shed_bulk:
        ops.capture(RuntimeError(f"RPOWER budget: tonight needs ~{int(demand)} requests for {stores} stores against "
                                 f"{int(nightly_budget)} in the nightly window at {rps:.2f} req/s — bulk work "
                                 f"(onboarding) waits until 7am" + (" and live labor reads less often outside "
                                                                    "decision windows" if stretch_live else "")),
                    job="pos_budget_check", context="backpressure")
    if over_nights == UTIL_ALERT_NIGHTS:
        ops.capture(RuntimeError(f"RPOWER nightly lane above {UTIL_ALERT_PCT:.0f}% of its budget "
                                 f"{UTIL_ALERT_NIGHTS} nights running (peak {peak:.0f}%)"),
                    job="pos_budget_check", context="utilisation")
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False, **{
        k: value[k] for k in ("stores", "demand", "budget", "shed_bulk", "stretch_live", "over_nights")}}


# ── queue sizing (Little's law) ─────────────────────────────────────────────

# Seconds one nightly request holds a slot: ~0.27 s RPOWER latency plus the
# row's own database work (A5 §3.1: ~8 s of CPU/DB per ~20-request store).
NIGHTLY_SECONDS_PER_REQUEST = _float_env("POS_IO_SECONDS_PER_REQUEST", 0.4)
# One live labor row: ~2.5 requests and ~1.2 s in all.
LIVE_SECONDS_PER_REQUEST = _float_env("POS_LIVE_SECONDS_PER_REQUEST", 0.48)
SLOT_HEADROOM = 1.5
MAX_POS_SLOTS = 32


def _slots(rps, seconds, floor):
    return int(min(MAX_POS_SLOTS, max(floor, math.ceil(rps * seconds * SLOT_HEADROOM))))


def pos_io_concurrency() -> int:
    """The pos_io group's slots (the nightly archive and sync rows) when
    QUEUE_POS_IO_CONCURRENCY is not set: the nightly window's share of the
    effective budget x seconds a request x headroom, never under 4 (A4-11)."""
    try:
        rps = effective_rps() * float(_shares()["nightly"].get("nightly", 0.8))
    except Exception:
        rps = fleet_rps() * 0.8
    return _slots(rps, NIGHTLY_SECONDS_PER_REQUEST, 4)


def live_concurrency() -> int:
    """The live group's slots (live labor's rows) when QUEUE_LIVE_CONCURRENCY
    is not set: service's live share of the budget x a live request's seconds
    x headroom, never under 2 (A5-03)."""
    try:
        rps = effective_rps() * share("live", _lane_now().replace(hour=19))
    except Exception:
        rps = fleet_rps() * 0.6
    return _slots(rps, LIVE_SECONDS_PER_REQUEST, 2)


# ── onboarding admission (A5-11, recommendation 9) ──────────────────────────

# What one store's onboarding costs (A5 §3.2: the 60-night onboard ≈ 516).
ONBOARD_REQUESTS = _float_env("POS_ONBOARD_REQUESTS_PER_STORE", 516)
# The share of the budget onboarding is admitted against (bulk borrows idle
# budget, so admission plans on half of it, A5's 5 req/s at 10).
ONBOARD_SHARE = _float_env("POS_ONBOARD_BUDGET_SHARE", 0.5)


def onboard_per_hour() -> float:
    """Stores whose onboarding may START an hour: POS_ONBOARD_PER_HOUR, else
    the budget's onboarding share over one store's requests (~35 at 10
    req/s)."""
    raw = (os.getenv("POS_ONBOARD_PER_HOUR") or "").strip()
    if raw:
        try:
            return max(0.1, float(raw))
        except ValueError:
            pass
    return max(1.0, effective_rps() * ONBOARD_SHARE * 3600.0 / max(1.0, ONBOARD_REQUESTS))


def onboard_not_before(now=None, db_path=None) -> float:
    """When the next onboarding row may start (epoch): spaced 3600 /
    onboard_per_hour() after the latest one already queued, and — while bulk
    is shed — not before the nightly window ends."""
    now = float(now or time.time())
    gap = 3600.0 / onboard_per_hour()
    last = None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT MAX(not_before) FROM jobs WHERE kind='pos_onboard' AND state='queued'"
                               ).fetchone()
        finally:
            conn.close()
        last = float(row[0]) if row and row[0] else None
    except Exception:
        last = None
    nb = now if last is None or last + gap <= now else last + gap
    if bulk_shed(db_path):
        until = state(db_path).get("until")
        if until and float(until) > nb:
            nb = float(until)
    return nb


# ── the request ledger's daily rollup (A5-13) ──────────────────────────────

LEDGER_HOURLY_DAYS = int(_float_env("POS_LEDGER_HOURLY_DAYS", 7))
LEDGER_ROLL_DAYS_PER_PASS = 30
_COUNTS = ("n", "ok", "rows_out", "s4xx", "s429", "s5xx", "transport", "skipped",
           "b250", "b1000", "b3000", "b10000", "bslow")


def roll_ledger_daily(db_path=None) -> int:
    """pos_request_ledger's hourly rows older than LEDGER_HOURLY_DAYS, one
    UTC day at a time (at most LEDGER_ROLL_DAYS_PER_PASS a pass), summed into
    one row per day, provider, store and endpoint whose `hour` is the date
    alone ("YYYY-MM-DD" sorts before that day's hours, so every `hour >=`
    window read is unchanged), and the hourly rows deleted in the same
    transaction. Returns the days rolled. Raises on a failed write (ops keeps
    the table's rows that night)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LEDGER_HOURLY_DAYS)).strftime("%Y-%m-%d 00:00:00")
    conn = get_conn(db_path)
    rolled = 0
    try:
        days = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT substr(hour, 1, 10) AS d FROM pos_request_ledger WHERE length(hour) > 10 AND hour < ? "
            "ORDER BY d LIMIT ?", (cutoff, LEDGER_ROLL_DAYS_PER_PASS)).fetchall()]
        sums = ", ".join(f"SUM({c}) AS {c}" for c in _COUNTS + ("ms_total",))
        cols = list(_COUNTS)
        adds = ", ".join(f"{c}=pos_request_ledger.{c}+excluded.{c}" for c in cols + ["ms_total"])
        upsert = (f"INSERT INTO pos_request_ledger (hour, provider, restaurant_id, endpoint, {', '.join(cols)}, "
                  f"ms_total, ms_max) VALUES ({', '.join('?' for _ in range(len(cols) + 6))}) "
                  f"ON CONFLICT(hour, provider, restaurant_id, endpoint) DO UPDATE SET {adds}, "
                  "ms_max=CASE WHEN excluded.ms_max > pos_request_ledger.ms_max THEN excluded.ms_max "
                  "ELSE pos_request_ledger.ms_max END")
        for d in days:
            lo, hi = f"{d} 00:00:00", f"{d} 23:59:59"
            rows = conn.execute(
                f"SELECT provider, restaurant_id, endpoint, {sums}, MAX(ms_max) AS ms_max FROM pos_request_ledger "
                "WHERE hour >= ? AND hour <= ? AND length(hour) > 10 GROUP BY provider, restaurant_id, endpoint",
                (lo, hi)).fetchall()
            batch = [(d, r["provider"], int(r["restaurant_id"] or 0), r["endpoint"],
                      *[int(r[c] or 0) for c in cols], round(float(r["ms_total"] or 0), 1),
                      round(float(r["ms_max"] or 0), 1)) for r in rows]
            if batch:
                conn.executemany(upsert, batch)
            conn.execute("DELETE FROM pos_request_ledger WHERE hour >= ? AND hour <= ? AND length(hour) > 10",
                         (lo, hi))
            conn.commit()
            rolled += 1
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return rolled
