"""
data_freshness.py — how current and complete each data source is, for one
restaurant. The Data Freshness dimension of a recommendation's confidence
(confidence_engine / rec_trust), Home's freshness strip and its "data as of"
line all read this one registry (contract K2).

For each source: the LAST DAY THE DATA COVERS (not the last write — a CSV of
shifts that ended 20 days ago, uploaded today, is 20 days old), a recency
score from one threshold table (SOURCES: grace and horizon in days), halved
when the source reports an error, times a completeness share (the share of
days in the window that carry sales, the share of ingredients ever counted).

    pct = 100 × recency × completeness
    recency = 1 within `grace` days of the expected lag, then linear to 0
              over `horizon` days; unknown age → 0 (never "current").

Before this module there were six freshness rules with thresholds of 1, 3,
8, 14 and 21 days, an unknown age read "fresh" on Home, and RPOWER — the POS
Simple EJ's runs on — was read by none of them (CA3 F1/F2/F6/F9/F15, CA6).

Never raises: an unreadable source is `unknown` with pct 0.
"""
from datetime import date, datetime, timedelta, timezone

import models as _models_mod
from models import DB_PATH
import confidence_engine as ce


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# The one threshold table. expected_lag: how old the data normally is when
# everything works (a nightly POS sync covers yesterday); grace: extra days
# before recency starts falling; horizon: days over which it falls to 0.
SOURCES = {
    "pos":        {"label": "POS",            "expected_lag": 1.0,  "grace": 0.5, "horizon": 7},
    "labor":      {"label": "Shifts",         "expected_lag": 1.0,  "grace": 1.0, "horizon": 7},
    "sales":      {"label": "Sales",          "expected_lag": 1.0,  "grace": 1.0, "horizon": 7},
    "reviews":    {"label": "Reviews",        "expected_lag": 0.25, "grace": 0.75, "horizon": 7},
    # ordering.COUNT_FRESH_DAYS: an order built on an older count is held.
    "inventory":  {"label": "Counts",         "expected_lag": 0.0,  "grace": 7.0, "horizon": 14},
    "purchases":  {"label": "Deliveries",     "expected_lag": 0.0,  "grace": 7.0, "horizon": 14},
    "marketing":  {"label": "Marketing",      "expected_lag": 0.0,  "grace": 10.0, "horizon": 14},
    "visibility": {"label": "AI visibility",  "expected_lag": 0.0,  "grace": 7.0, "horizon": 14},
    "competitor": {"label": "Competitors",    "expected_lag": 0.0,  "grace": 7.0, "horizon": 14},
    "weather":    {"label": "Weather",        "expected_lag": 0.0,  "grace": 1.0, "horizon": 2},
    "dsr":        {"label": "Daily report",   "expected_lag": 1.0,  "grace": 1.0, "horizon": 7},
}
# A source with an error (a failing sync, an expired token, two missed
# review fetches) is at most half fresh.
ERROR_CEILING = 0.5
REVIEW_SLOTS_MISSED_AT = 2
LABOR_WINDOW_DAYS = 28

# Which sources each module's recommendations rest on.
MODULE_SOURCES = {
    "reviews": ("reviews",),
    "labor": ("labor", "pos"),
    "schedule": ("labor", "pos"),
    "inventory": ("inventory", "pos"),
    "food": ("inventory", "pos"),
    "food_cost": ("inventory", "pos"),
    "marketing": ("marketing",),
    "intel": ("competitor",),
    "visibility": ("visibility",),
    "ops": ("dsr",),
    "dsr": ("dsr",),
    "daily report": ("dsr",),       # Ask's label for the DSR tools
}


def sources_for(modules) -> tuple:
    out = []
    for m in modules or ():
        for s in MODULE_SOURCES.get(str(m or "").lower(), ()):
            if s not in out:
                out.append(s)
    return tuple(out)


def _get(r, name):
    if r is None:
        return None
    if isinstance(r, dict):
        return r.get(name)
    try:
        return r[name]
    except Exception:
        return getattr(r, name, None)


def _rid(r):
    return _get(r, "id")


def _today(r, now=None):
    """The restaurant's local calendar date."""
    from time_utils import restaurant_tz
    tzname = _get(r, "timezone")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(restaurant_tz(tzname or None)).date()


def _as_date(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


# ── adapters onto the data-quality group's helpers ─────────────────────────
#
# Group G (confidence audit, data quality at the source) added
# time_utils.parse_stamp, fetcher.places_coverage, admin_ops.review_source,
# weather.cache_age and scheduler.metrics_sync_state; each is read through one
# adapter here. The "not merged yet" hasattr fallbacks were removed at the
# integration pass (trace: every helper is defined unconditionally —
# time_utils.py parse_stamp, fetcher.py places_coverage, admin_ops.py
# review_source; no test deletes or patches them away; no dynamic lookup
# reaches the guards; pos_health.parse_stamp, the old fallback, is itself a
# wrapper over time_utils.parse_stamp). The exception fallbacks stay.

def _stamp(v, naive_tz="UTC"):
    """A stored timestamp as an aware UTC datetime, or None — the one
    parser for every stamp style (time_utils.parse_stamp, CA3 F15)."""
    from time_utils import parse_stamp
    return parse_stamp(v, naive_tz=naive_tz)


def _review_fetched_at(raw):
    """restaurants.last_fetched_at (Chicago local with a 'T', or SQLite UTC)
    as an aware datetime, or None."""
    import admin_ops
    return admin_ops.fetched_at_ct(raw)


def _review_sampling(r):
    """(sampled, completeness or None, label) for the review source: Places
    returns five reviews at a time, so a Places-only connection is a sample;
    its completeness is fetcher.places_coverage's share of Google's own count
    growth when that helper exists (CA3 F13)."""
    sampled = bool(_get(r, "reviews_live")) and not _get(r, "gmb_refresh_token")
    label = "Google reviews (sampled — Places returns 5 at a time)" if sampled else "Google Business Profile"
    try:
        import admin_ops
        kind, label = admin_ops.review_source(r)
        sampled = kind == "places_sampled"
    except Exception:
        pass            # the row-derived reading above stands
    share = None
    if sampled:
        try:
            import fetcher
            cov = fetcher.places_coverage(_rid(r)) or {}
            if cov.get("share") is not None:
                share = max(0.0, min(1.0, float(cov["share"])))
        except Exception:
            share = None
    return sampled, share, label


def _result(key, pct=None, as_of_iso=None, basis="", state=None, error=None, **extra):
    out = {"key": key, "label": SOURCES.get(key, {}).get("label", key), "pct": pct,
           "as_of": ce._mdy(as_of_iso) or None, "as_of_iso": as_of_iso, "basis": basis,
           "state": state or ce.state(pct), "error": error}
    out.update(extra)
    return out


def _score(key, lag_days, completeness=1.0, error=False):
    cfg = SOURCES[key]
    r = ce.recency(None if lag_days is None else max(0.0, lag_days - cfg["expected_lag"]),
                   cfg["grace"], cfg["horizon"])
    if error:
        r = min(r, ERROR_CEILING)
    c = 1.0 if completeness is None else max(0.0, min(1.0, float(completeness)))
    return int(round(100 * r * c))


def _data_date_state(key, d, today, basis_word, completeness=1.0, extra_basis="", error=None):
    """A source dated by the last day its data covers."""
    if d is None:
        return _result(key, 0, None, f"{SOURCES[key]['label']}: no dated data on file", state="unknown",
                       error=error)
    lag = (today - d).days
    pct = _score(key, lag, completeness, bool(error))
    basis = f"{basis_word} {ce._mdy(d.isoformat())}" + (f" · {extra_basis}" if extra_basis else "")
    return _result(key, pct, d.isoformat(), basis, error=error, lag_days=lag)


# ── the sources ────────────────────────────────────────────────────────────

def _pos(r, conn, today, now, ctx):
    import pos_health
    s = pos_health.pos_sync_state(r, now=now)
    if not s.get("connected"):
        return _result("pos", None, None, "No POS connected", state="not_connected")
    name = {"rpower": "RPOWER"}.get(s["provider"], (s["provider"] or "POS").title())
    if not s.get("last_synced_iso"):
        return _result("pos", 0, None, f"{name} has never synced", state="unknown", error=s.get("error"),
                       provider=s["provider"])
    age = s.get("age_days")
    pct = _score("pos", age, 1.0, bool(s.get("error")))
    when = ce._mdy(s["last_synced_iso"])
    basis = (f"{name} sync failing — last good sync {when}" if s.get("error") else f"{name} synced {when}")
    return _result("pos", pct, s["last_synced_iso"], basis, error=s.get("error"), provider=s["provider"])


def _labor(r, conn, today, now, ctx):
    """Shifts: dated by the analysis's last shift day (date_range.end) when
    the caller has the analysis, else the last day in the daily archive
    with labor cost — never client_data.updated_at, which any upload
    (inventory included) moves (CA3 F2). Completeness: the share of the
    window's shift days that carry sales."""
    a = (ctx or {}).get("labor")
    end, comp, extra = None, 1.0, ""
    if a and a.get("is_live"):
        end = _as_date((a.get("date_range") or {}).get("end"))
        days = int((a.get("date_range") or {}).get("days") or a.get("period_days") or 0)
        missing = len(a.get("days_missing_sales") or [])
        if days:
            comp = max(0.0, (days - missing) / float(days))
            if missing:
                extra = f"{days - missing} of {days} days carry sales"
    else:
        row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                           "AND labor_cost > 0", (_rid(r),)).fetchone()
        end = _as_date(row["d"] if row else None)
        if end is not None:
            start = (end - timedelta(days=LABOR_WINDOW_DAYS - 1)).isoformat()
            c = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN sales IS NOT NULL AND sales > 0 THEN 1 ELSE 0 END) "
                             "AS s FROM labor_daily_history WHERE restaurant_id=? AND labor_cost > 0 "
                             "AND date BETWEEN ? AND ?", (_rid(r), start, end.isoformat())).fetchone()
            n, s = int(c["n"] or 0), int(c["s"] or 0)
            if n:
                comp = s / float(n)
                if s < n:
                    extra = f"{s} of {n} days carry sales"
    if end is None and not (a and a.get("is_live")):
        # No shifts on file at all: the source does not apply yet (a
        # restaurant that never uploaded is not "stale"). Data on file with
        # no date is `unknown` — never fresh.
        return _result("labor", None, None, "No shifts on file", state="not_connected")
    return _data_date_state("labor", end, today, "Shifts through", comp, extra)


def _sales(r, conn, today, now, ctx):
    row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                       "AND sales IS NOT NULL AND sales > 0", (_rid(r),)).fetchone()
    d = _as_date(row["d"] if row else None)
    if d is None:
        return _result("sales", None, None, "No sales on file", state="not_connected")
    comp = 1.0
    if d is not None:
        try:
            import metrics
            cov = metrics.coverage(_rid(r), "sales", (d - timedelta(days=LABOR_WINDOW_DAYS - 1)).isoformat(),
                                   d.isoformat())
            if cov and cov.get("expected"):
                comp = float(cov["share"])
        except Exception:
            comp = 1.0
    return _data_date_state("sales", d, today, "Sales through", comp,
                            "" if comp >= 1 else f"{int(round(comp * 100))}% of trading days carry sales")


def _reviews(r, conn, today, now, ctx):
    """Dated by the last fetch (restaurants.last_fetched_at, Chicago 'T' or
    SQLite UTC — admin_ops.fetched_at_ct reads both); two or more missed
    fetch slots is an error. A Places-only connection is labelled sampled:
    Places returns five reviews at a time."""
    connected = bool(_get(r, "gmb_refresh_token") or _get(r, "reviews_live"))
    if not connected:
        return _result("reviews", None, None, "Google not connected", state="not_connected")
    import admin_ops
    raw = _get(r, "last_fetched_at")
    at = _review_fetched_at(raw)
    if at is None:
        return _result("reviews", 0, None, "Reviews never fetched", state="unknown")
    missed = admin_ops.fetch_slots_missed(raw)
    err = (f"{missed} review fetches missed" if (missed or 0) >= REVIEW_SLOTS_MISSED_AT else None)
    age = max(0.0, (now - at.astimezone(timezone.utc)).total_seconds() / 86400.0)
    sampled, share, label = _review_sampling(r)
    pct = _score("reviews", age, share if share is not None else 1.0, bool(err))
    day = at.date().isoformat()
    basis = f"Reviews fetched {ce._mdy(day)}" + (f" — {err}" if err else "") + (
        f" · {label}" if sampled else "") + (
        f" · {int(round(share * 100))}% of Google's new reviews stored" if share is not None else "")
    return _result("reviews", pct, day, basis, error=err, sampled=sampled)


def _inventory(r, conn, today, now, ctx):
    """The OLDEST count among the ingredients that have been counted
    (ingredients.last_recount_at — restaurants.inventory_updated_at is never
    written, CA3 F9), times the share of active ingredients ever counted."""
    row = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN last_recount_at IS NOT NULL AND last_recount_at != '' "
                       "THEN 1 ELSE 0 END) AS counted, MIN(NULLIF(last_recount_at, '')) AS oldest "
                       "FROM ingredients WHERE restaurant_id=? AND is_active=1", (_rid(r),)).fetchone()
    n, counted = int(row["n"] or 0), int(row["counted"] or 0)
    if not n:
        return _result("inventory", None, None, "No ingredients on file", state="not_connected")
    d = _as_date(row["oldest"])
    comp = counted / float(n) if n else 0.0
    extra = f"{counted} of {n} items counted" if counted < n else ""
    return _data_date_state("inventory", d, today, "Oldest count", comp, extra)


def _purchases(r, conn, today, now, ctx):
    row = conn.execute("SELECT MAX(event_date) AS d FROM ingredient_stock_events WHERE restaurant_id=? "
                       "AND event_type='receiving'", (_rid(r),)).fetchone()
    d = _as_date(row["d"] if row else None)
    if d is None:
        return _result("purchases", None, None, "No deliveries logged", state="not_connected")
    return _data_date_state("purchases", d, today, "Last delivery logged")


def _marketing(r, conn, today, now, ctx):
    """Post metrics need a live token: an expired Instagram or Facebook token
    is an error, whatever was last posted (CA3 F15 — "fresh" whenever a
    token existed)."""
    ig, fb = _get(r, "ig_token"), _get(r, "fb_page_token")
    if not ig and not fb:
        return _result("marketing", None, None, "No social account connected", state="not_connected")
    err = None
    for name, tok, exp in (("Instagram", ig, _get(r, "ig_token_expires")), ("Facebook", fb, _get(r, "fb_token_expires"))):
        e = _stamp(exp) if (tok and exp) else None
        if e is not None and e <= now:
            err = f"{name} token expired {ce._mdy(e.date().isoformat())}"
            break
    row = conn.execute("SELECT MAX(COALESCE(posted_at, created_at)) AS t FROM marketing_content_log "
                       "WHERE restaurant_id=? AND post_id IS NOT NULL", (_rid(r),)).fetchone()
    d = _as_date(row["t"] if row else None)
    if d is None:
        return _result("marketing", 0 if err else None, None, err or "Nothing posted yet",
                       state="stale" if err else "not_connected", error=err)
    # The post figures (reach, engagement) are only as current as the
    # nightly metrics sync that refreshes them (CA3 F15, G11): a sync that
    # is failing, has never succeeded, or last succeeded more than
    # METRICS_SYNC_STALE_DAYS ago is an error on this source, whatever the
    # token says.
    sync = _metrics_sync(r, conn, ctx)
    note = ""
    if sync:
        ok = _stamp(sync.get("last_ok_at"))
        if sync.get("error"):
            err = err or f"Post metrics sync failing ({str(sync['error'])[:80]})"
        elif ok is None:
            err = err or "Post metrics have never synced"
        elif (now - ok).total_seconds() / 86400.0 > METRICS_SYNC_STALE_DAYS:
            err = err or f"Post metrics last synced {ce._mdy(ok.date().isoformat())}"
        if ok is not None:
            note = f"metrics synced {ce._mdy(ok.date().isoformat())}"
    extra = " · ".join(x for x in (err, note) if x)
    out = _data_date_state("marketing", d, today, "Last post", 1.0, extra, error=err)
    out["metrics_synced_at"] = sync.get("last_ok_at") if sync else None
    return out


# The Meta metrics sync is nightly (scheduler.run_marketing_metrics_sync); a
# success older than this has missed a night.
METRICS_SYNC_STALE_DAYS = 2


def _metrics_sync(r, conn, ctx):
    """scheduler.metrics_sync_state for this restaurant, read through this
    reading's own connection — {} when it never ran. A deliberate
    function-scope upward import (L2 → L4, ARCHITECTURE_MANIFEST §3; pos.py
    does the same): the scheduler owns the job_cursors key it writes, so it
    owns the reader too. `ctx["metrics_sync"]` overrides (a caller that
    already read it). Never raises."""
    if isinstance(ctx, dict) and "metrics_sync" in ctx:
        return ctx.get("metrics_sync") or {}
    try:
        import scheduler
        return scheduler.metrics_sync_state(_rid(r), conn=conn) or {}
    except Exception as e:
        print(f"[data_freshness] metrics sync state unreadable for {_rid(r)}: {e}")
        return {}


def _visibility(r, conn, today, now, ctx):
    try:
        row = conn.execute("SELECT MAX(created_at) AS t FROM ai_visibility_runs WHERE restaurant_id=?",
                           (_rid(r),)).fetchone()
    except Exception:
        row = None
    d = _as_date(row["t"] if row else None)
    if d is None:
        return _result("visibility", None, None, "AI visibility not checked yet", state="not_connected")
    return _data_date_state("visibility", d, today, "AI visibility checked")


def _competitor(r, conn, today, now, ctx):
    d = _as_date(_get(r, "competitor_updated_at"))
    if d is None:
        return _result("competitor", None, None, "No competitor read yet", state="not_connected")
    return _data_date_state("competitor", d, today, "Competitors read")


def _weather(r, conn, today, now, ctx):
    """The cached forecast's age in hours, read by weather.py's own rule
    (G11: weather_cached_at is UTC with an offset now, server-local naive
    on older rows) and its own line — `stale` past
    weather.FORECAST_STALE_HOURS, or when the age is unknown. A stale copy
    is an error on this source (at most half fresh), so Home never calls a
    72-hour-old fallback forecast current."""
    raw = _get(r, "weather_cached_at")
    if not raw:
        return _result("weather", None, None, "No forecast cached", state="not_connected")
    import weather
    at = _stamp(raw, naive_tz="local")
    age_h = max(0.0, (now - at).total_seconds() / 3600.0) if at is not None else None
    stale = age_h is None or age_h > weather.FORECAST_STALE_HOURS
    if at is None:
        return _result("weather", 0, None, "Weather: forecast age unknown", state="unknown",
                       error="forecast age unknown", age_hours=None, stale=True)
    err = f"Forecast is {int(age_h)} hours old" if stale else None
    iso = at.date().isoformat()
    pct = _score("weather", age_h / 24.0, 1.0, bool(err))
    basis = f"Forecast cached {ce._mdy(iso)}" + (f" · {err}" if err else "")
    return _result("weather", pct, iso, basis, error=err, age_hours=round(age_h, 1), stale=stale,
                   lag_days=round(age_h / 24.0, 2))


def _dsr(r, conn, today, now, ctx):
    try:
        row = conn.execute("SELECT MAX(business_date) AS d FROM dsr_reports WHERE restaurant_id=? "
                           "AND finalized_at IS NOT NULL", (_rid(r),)).fetchone()
    except Exception:
        row = None
    d = _as_date(row["d"] if row else None)
    if d is None:
        return _result("dsr", None, None, "No daily report yet", state="not_connected")
    return _data_date_state("dsr", d, today, "Daily report for")


_READERS = {"pos": _pos, "labor": _labor, "sales": _sales, "reviews": _reviews, "inventory": _inventory,
            "purchases": _purchases, "marketing": _marketing, "visibility": _visibility,
            "competitor": _competitor, "weather": _weather, "dsr": _dsr}


def source_state(restaurant, key, db_path=None, now=None, context=None) -> dict:
    """{key, label, pct, as_of, as_of_iso, basis, state, error} for one
    source. state: current | aging | stale (from pct, confidence_engine.state)
    | not_connected (pct None — the source does not apply) | unknown (no
    date: pct 0, never fresh). Never raises."""
    if key not in _READERS:
        return _result(key, None, None, "Unknown source", state="unknown")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[data_freshness] no connection: {e}")
        return _result(key, 0, None, f"{SOURCES[key]['label']}: could not be read", state="unknown")
    try:
        return _READERS[key](restaurant, conn, _today(restaurant, now), now, context)
    except Exception as e:
        print(f"[data_freshness] {key} unreadable for {_rid(restaurant)}: {e}")
        return _result(key, 0, None, f"{SOURCES[key]['label']}: could not be read", state="unknown")
    finally:
        conn.close()


def states(restaurant, keys, db_path=None, now=None, context=None, cache=None) -> list:
    """source_state for each key, read once per `cache` dict (one Home build)."""
    out = []
    for k in keys or ():
        if cache is not None and k in cache:
            out.append(cache[k])
            continue
        s = source_state(restaurant, k, db_path=db_path, now=now, context=context)
        if cache is not None:
            cache[k] = s
        out.append(s)
    return out
