"""
data_freshness.py — how current and complete each data source is, for one
restaurant. The Data Freshness dimension of a recommendation's confidence
(confidence_engine / rec_trust), Home's freshness strip and its "data as of"
line all read this one registry (contract K2).

For each source: the LAST DAY THE DATA COVERS (not the last write — a CSV of
shifts that ended 20 days ago, uploaded today, is 20 days old; a POS is
dated by its last business date carrying sales, not by the sync stamp), a
recency score from one threshold table (SOURCES: grace and horizon in days),
held under ERROR_CEILING — strictly below the engine's stale threshold —
when the source reports an error, times a completeness share where one is
measured outside Evidence Strength (the share of ingredients ever counted,
Places' share of Google's new reviews). Days without sales are NOT a
freshness completeness: they are counted once, in Evidence Strength.

    pct = 100 × recency × completeness
    recency = 1 within `grace` days of the expected lag, then linear to 0
              over `horizon` days; unknown age → 0 (never "current"); a
              date past the restaurant's today (FUTURE_DAYS) → unknown.

pos_health's current / aging / stale is this table's "pos" row (age_pct),
so every surface names a POS state by one rule.

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
# review fetches, a stale weather copy) is held STRICTLY below the engine's
# stale threshold (confidence_engine.STALE_BELOW = 50), so an erroring source
# always trips the stale cap and its caution. At 0.5 it sat exactly on 50,
# the `f < 50` test never fired, and a 401 on the POS read the same overall %
# as a healthy sync (re-audit B3#2). Held below STALE_BELOW by a test.
ERROR_CEILING = 0.45
REVIEW_SLOTS_MISSED_AT = 2
LABOR_WINDOW_DAYS = 28
# A data date or stamp more than this many days past the restaurant's local
# today is not data from the future: it is a typo or a clock error, read as
# `unknown` (never "current") — one 2027 row in a shifts CSV read 100% fresh
# and anchored every labor read on a one-day window (re-audit B3#4, #14).
FUTURE_DAYS = 1

# Which sources each module's recommendations rest on. `sales` (the last
# business date carrying sales) rides with every module whose figures divide
# by sales — labor %, food cost %, the schedule's sales per labor hour, the
# daily report — so a POS that keeps syncing while its sales stop arriving
# is seen (re-audit B3#3). `weather` rides with the recommendations built
# on a forecast (the schedule's rain trim, demand). A source that does not
# apply to a restaurant reads pct None and is left out of the minimum.
MODULE_SOURCES = {
    "reviews": ("reviews",),
    "labor": ("labor", "pos", "sales"),
    "schedule": ("labor", "pos", "sales", "weather"),
    "demand": ("sales", "pos", "weather"),
    "inventory": ("inventory", "pos", "sales"),
    "food": ("inventory", "pos", "sales"),
    "food_cost": ("inventory", "pos", "sales"),
    "marketing": ("marketing",),
    # Guest campaigns measured by guests who came back: matched through the
    # POS's orders (guest_marketing._with_confidence).
    "campaigns": ("pos", "sales"),
    "intel": ("competitor",),
    "visibility": ("visibility",),
    "ops": ("dsr", "sales"),
    "dsr": ("dsr", "sales"),
    "daily report": ("dsr", "sales"),       # Ask's label for the DSR tools
}

# Ask tools whose module label names no source (or not all of them): what
# each actually reads (re-audit B3#13). read_business_snapshot's stand-in
# covers every module it can read until it reports which it did; outcomes,
# goals and decisions are measured from the metrics archive (sales, labor,
# reviews); the platform read places this restaurant by its own labor,
# sales, rating and food cost; the demand forecast is built from sales and
# the weather.
TOOL_SOURCES = {
    "read_business_snapshot": ("reviews", "labor", "pos", "sales", "inventory", "marketing", "visibility"),
    "read_outcomes": ("sales", "labor", "reviews"),
    "read_goals": ("sales", "labor", "reviews"),
    "read_decisions": ("sales", "labor", "reviews"),
    "read_platform_intelligence": ("labor", "sales", "reviews", "inventory"),
    "read_demand_forecast": ("sales", "pos", "weather"),
}


def sources_for(modules) -> tuple:
    out = []
    for m in modules or ():
        for s in MODULE_SOURCES.get(str(m or "").lower(), ()):
            if s not in out:
                out.append(s)
    return tuple(out)


def sources_for_tools(tool_names, modules=()) -> tuple:
    """The sources an Ask answer rests on: its modules' (sources_for) plus
    what each tool in TOOL_SOURCES reads."""
    out = list(sources_for(modules))
    for name in tool_names or ():
        for s in TOOL_SOURCES.get(str(name or ""), ()):
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


def review_evidence_flags(restaurant) -> tuple:
    """The Evidence Strength flags every review-built confidence carries:
    ("sampled",) when the reviews arrive through Places only (five at a
    time — admin_ops.review_source's "places_sampled"), else (). The ONE
    helper Home's review card, the Reviews page diagnosis, Do-today and the
    one-thing hero read, so the same diagnosis no longer scores 74 on Home
    and 100 on the Reviews page (re-audit B3#7, B4 M1). Never raises."""
    try:
        import admin_ops
        kind, _label = admin_ops.review_source(restaurant if restaurant is not None else {})
        return ("sampled",) if kind == "places_sampled" else ()
    except Exception:
        return ()


def _review_sampling(r, db_path=None):
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
            cov = fetcher.places_coverage(_rid(r), db_path=db_path) or {}
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


def age_pct(key, lag_days, completeness=1.0, error=False):
    """A source's freshness percentage for data `lag_days` old — the one
    threshold rule (SOURCES) every surface reads, pos_health's state
    included. An error holds it under ERROR_CEILING."""
    cfg = SOURCES[key]
    r = ce.recency(None if lag_days is None else max(0.0, lag_days - cfg["expected_lag"]),
                   cfg["grace"], cfg["horizon"])
    if error:
        r = min(r, ERROR_CEILING)
    c = 1.0 if completeness is None else max(0.0, min(1.0, float(completeness)))
    return int(round(100 * r * c))


_score = age_pct


def _future(d, today):
    """True when data date `d` is past the restaurant's today + FUTURE_DAYS."""
    return d is not None and today is not None and (d - today).days > FUTURE_DAYS


def _latest_ok(today):
    """The latest ISO date a data row may carry and still count."""
    return (today + timedelta(days=FUTURE_DAYS)).isoformat()


def _data_date_state(key, d, today, basis_word, completeness=1.0, extra_basis="", error=None):
    """A source dated by the last day its data covers. A date past today
    (FUTURE_DAYS) is unknown — never current."""
    if d is None:
        return _result(key, 0, None, f"{SOURCES[key]['label']}: no dated data on file", state="unknown",
                       error=error)
    if _future(d, today):
        return _result(key, 0, None,
                       f"{SOURCES[key]['label']}: dated {ce._mdy(d.isoformat())}, after today — a typo or "
                       "clock error, so its age is unknown", state="unknown",
                       error=error or "dated in the future", future=True)
    lag = (today - d).days
    pct = _score(key, lag, completeness, bool(error))
    basis = f"{basis_word} {ce._mdy(d.isoformat())}" + (f" · {extra_basis}" if extra_basis else "")
    return _result(key, pct, d.isoformat(), basis, error=error, lag_days=lag)


# ── the sources ────────────────────────────────────────────────────────────
#
# Every reader takes (restaurant row, open connection, restaurant-local
# today, aware now, context, db_path) and never raises past source_state.

def _pos(r, conn, today, now, ctx, db_path=None):
    """Dated by the LAST BUSINESS DATE CARRYING SALES in the daily archive
    (labor_daily_history, which every provider's sync writes through
    pos.save_synced_shifts) — the last day the POS data covers, the rule
    every other source follows — not the sync stamp: a sync that ran every
    night while its sales call returned nothing kept the POS 100% current
    (re-audit B3#3). The stamp still says whether it ever synced and when;
    the error column whether it is failing.

    A POS with credentials that has never synced is `unknown` with pct None:
    no card's figures came from it yet, so it must not zero a card built
    from uploaded shifts (B1 H1a — connecting Toast before its first sync
    dropped every labor card to 0%). With nothing archived yet the sync
    stamp dates it, as before. A stamp in the future is unknown, pct 0."""
    import pos_health
    s = pos_health.pos_sync_state(r, now=now)
    if not s.get("connected"):
        return _result("pos", None, None, "No POS connected", state="not_connected")
    name = {"rpower": "RPOWER"}.get(s["provider"], (s["provider"] or "POS").title())
    err = s.get("error")
    if s.get("future") or s.get("unreadable"):
        what = "is after now — a clock or data error" if s.get("future") else "can't be read"
        return _result("pos", 0, None, f"{name} sync time {what}, so its age is unknown", state="unknown",
                       error=err or ("sync stamp in the future" if s.get("future") else "sync stamp unreadable"),
                       provider=s["provider"], future=bool(s.get("future")))
    if not s.get("last_synced_iso"):
        return _result("pos", None, None, f"{name} has never synced" + (f" ({str(err)[:80]})" if err else ""),
                       state="unknown", error=err, provider=s["provider"], never_synced=True)
    when = ce._mdy(s["last_synced_iso"])
    today = today or _today(r, now)
    d = None
    if conn is not None:
        row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                           "AND sales IS NOT NULL AND sales > 0 AND date <= ?",
                           (_rid(r), _latest_ok(today))).fetchone()
        d = _as_date(row["d"] if row else None)
    if d is None:
        pct = age_pct("pos", s.get("age_days"), 1.0, bool(err))
        basis = (f"{name} sync failing — last good sync {when}" if err else f"{name} synced {when}")
        return _result("pos", pct, s["last_synced_iso"], basis, error=err, provider=s["provider"],
                       last_synced_iso=s["last_synced_iso"])
    extra = (f"sync failing — last good sync {when}" if err else f"synced {when}")
    out = _data_date_state("pos", d, today, f"{name} sales through", 1.0, extra, error=err)
    out.update(provider=s["provider"], last_synced_iso=s["last_synced_iso"])
    return out


# Missing sales days are penalised ONCE (re-audit B3#10): in Evidence
# Strength — the evidence input's coverage (days with sales ÷ shift days)
# and its `days_missing_sales` partial flag (labor.diagnosis_evidence_input,
# Home's labor evidence). Data Freshness for `labor` and `sales` measures
# only how recent the data is; the count of days without sales stays in the
# basis as a note and no longer multiplies the percentage (it did, so the
# same ten missing days read evidence 41 AND freshness 64).

def _labor(r, conn, today, now, ctx, db_path=None):
    """Shifts: dated by the analysis's last shift day (date_range.end) when
    the caller has the analysis, else the last day in the daily archive
    with labor cost — never client_data.updated_at, which any upload
    (inventory included) moves (CA3 F2). A day past today (FUTURE_DAYS) is
    a typo and never dates the source."""
    a = (ctx or {}).get("labor")
    end, extra = None, ""
    if a and a.get("is_live"):
        end = _as_date((a.get("date_range") or {}).get("end"))
        days = int((a.get("date_range") or {}).get("days") or a.get("period_days") or 0)
        missing = len(a.get("days_missing_sales") or [])
        if days and missing:
            extra = f"{max(0, days - missing)} of {days} days carry sales"
    else:
        row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                           "AND labor_cost > 0 AND date <= ?", (_rid(r), _latest_ok(today))).fetchone()
        end = _as_date(row["d"] if row else None)
        if end is not None:
            start = (end - timedelta(days=LABOR_WINDOW_DAYS - 1)).isoformat()
            c = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN sales IS NOT NULL AND sales > 0 THEN 1 ELSE 0 END) "
                             "AS s FROM labor_daily_history WHERE restaurant_id=? AND labor_cost > 0 "
                             "AND date BETWEEN ? AND ?", (_rid(r), start, end.isoformat())).fetchone()
            n, s = int(c["n"] or 0), int(c["s"] or 0)
            if n and s < n:
                extra = f"{s} of {n} days carry sales"
    if end is None and not (a and a.get("is_live")):
        # No shifts on file at all: the source does not apply yet (a
        # restaurant that never uploaded is not "stale"). Data on file with
        # no date is `unknown` — never fresh.
        return _result("labor", None, None, "No shifts on file", state="not_connected")
    return _data_date_state("labor", end, today, "Shifts through", 1.0, extra)


def _sales(r, conn, today, now, ctx, db_path=None):
    """The last business date carrying sales (NULL and legacy 0 rows are no
    sales). Recency only — see the note above _labor."""
    row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                       "AND sales IS NOT NULL AND sales > 0 AND date <= ?",
                       (_rid(r), _latest_ok(today))).fetchone()
    d = _as_date(row["d"] if row else None)
    if d is None:
        return _result("sales", None, None, "No sales on file", state="not_connected")
    note = ""
    try:
        import metrics
        kw = {"db_path": db_path} if db_path else {}
        cov = metrics.coverage(_rid(r), "sales", (d - timedelta(days=LABOR_WINDOW_DAYS - 1)).isoformat(),
                               d.isoformat(), **kw)
        if cov and cov.get("expected") and float(cov["share"]) < 1:
            note = f"{int(round(float(cov['share']) * 100))}% of trading days carry sales"
    except Exception:
        note = ""
    return _data_date_state("sales", d, today, "Sales through", 1.0, note)


def _reviews(r, conn, today, now, ctx, db_path=None):
    """Dated by the last fetch (restaurants.last_fetched_at, Chicago 'T' or
    SQLite UTC — admin_ops.fetched_at_ct reads both); two or more missed
    fetch slots, counted against the `now` this reading was asked about, is
    an error. A Places-only connection is labelled sampled: Places returns
    five reviews at a time."""
    f = review_fetch_state(r, now=now)
    if f["state"] in ("not_connected", "unknown"):
        return _result("reviews", f["pct"], None, f["basis"], state=f["state"], error=f["error"],
                       future=f["future"])
    err, age, day = f["error"], f["age_days"], f["as_of_iso"]
    sampled, share, label = _review_sampling(r, db_path=db_path)
    pct = age_pct("reviews", age, share if share is not None else 1.0, bool(err))
    basis = f"Reviews fetched {ce._mdy(day)}" + (f" — {err}" if err else "") + (
        f" · {label}" if sampled else "") + (
        f" · {int(round(share * 100))}% of Google's new reviews stored" if share is not None else "")
    return _result("reviews", pct, day, basis, error=err, sampled=sampled)


def review_fetch_state(restaurant, now=None) -> dict:
    """The recency half of the reviews source, from the row alone (no
    database read): {pct, state, age_days, as_of, as_of_iso, error, missed,
    future, basis}. Home's "Reviews haven't refreshed" nudge and the
    portfolio strip read this instead of their own 3-day cut (re-audit
    B3#9), so they flag exactly when every review card's Data Freshness
    reads stale. Never raises."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out = {"pct": None, "state": "not_connected", "age_days": None, "as_of": None, "as_of_iso": None,
           "error": None, "missed": None, "future": False, "basis": "Google not connected"}
    try:
        if not (_get(restaurant, "gmb_refresh_token") or _get(restaurant, "reviews_live")):
            return out
        import admin_ops
        from zoneinfo import ZoneInfo
        raw = _get(restaurant, "last_fetched_at")
        at = _review_fetched_at(raw)
        if at is None:
            out.update(pct=0, state="unknown", basis="Reviews never fetched")
            return out
        if (at - now).total_seconds() > 3600:
            out.update(pct=0, state="unknown", future=True, error="fetch stamp in the future",
                       basis="Reviews: last fetch is dated after now — a clock or data error, so its age is unknown")
            return out
        missed = admin_ops.fetch_slots_missed(raw, now=now.astimezone(ZoneInfo("America/Chicago")))
        err = (f"{missed} review fetches missed" if (missed or 0) >= REVIEW_SLOTS_MISSED_AT else None)
        age = max(0.0, (now - at.astimezone(timezone.utc)).total_seconds() / 86400.0)
        pct = age_pct("reviews", age, 1.0, bool(err))
        day = at.date().isoformat()
        out.update(pct=pct, state=ce.state(pct), age_days=round(age, 2), as_of=ce._mdy(day), as_of_iso=day,
                   error=err, missed=missed, basis=f"Reviews fetched {ce._mdy(day)}" + (f" — {err}" if err else ""))
    except Exception as e:
        print(f"[data_freshness] review fetch state unreadable for {_rid(restaurant)}: {e}")
        out.update(pct=0, state="unknown", basis="Reviews: could not be read")
    return out


def _inventory(r, conn, today, now, ctx, db_path=None):
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


def _purchases(r, conn, today, now, ctx, db_path=None):
    row = conn.execute("SELECT MAX(event_date) AS d FROM ingredient_stock_events WHERE restaurant_id=? "
                       "AND event_type='receiving'", (_rid(r),)).fetchone()
    d = _as_date(row["d"] if row else None)
    if d is None:
        return _result("purchases", None, None, "No deliveries logged", state="not_connected")
    return _data_date_state("purchases", d, today, "Last delivery logged")


def _marketing(r, conn, today, now, ctx, db_path=None):
    """Post metrics need a live token: an expired Instagram or Facebook token
    is an error, whatever was last posted (CA3 F15 — "fresh" whenever a
    token existed). An expiry that can't be read is `unknown`, never
    unexpired (re-audit B3#5). A token with no expiry stored (a long-lived
    page token) is read as live."""
    ig, fb = _get(r, "ig_token"), _get(r, "fb_page_token")
    if not ig and not fb:
        return _result("marketing", None, None, "No social account connected", state="not_connected")
    err = None
    for name, tok, exp in (("Instagram", ig, _get(r, "ig_token_expires")), ("Facebook", fb, _get(r, "fb_token_expires"))):
        if not (tok and exp):
            continue
        e = _stamp(exp)
        if e is None:
            return _result("marketing", 0, None, f"{name} token expiry can't be read, so whether its post "
                           "metrics are current is unknown", state="unknown",
                           error=f"{name} token expiry unreadable")
        if e <= now:
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
    # is failing, has never run for this connected account (no state at
    # all — re-audit B3#5: it read 100%), has never succeeded, or last
    # succeeded more than METRICS_SYNC_STALE_DAYS ago is an error on this
    # source, whatever the token says.
    sync = _metrics_sync(r, conn, ctx)
    note = ""
    if sync is None:
        err = err or "Post metrics sync state could not be read"
    elif not sync:
        err = err or "Post metrics have never synced"
    else:
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
    reading's own connection — {} when it never ran, None when it could not
    be read. A deliberate function-scope upward import (L2 → L4,
    ARCHITECTURE_MANIFEST §3; pos.py does the same): the scheduler owns the
    job_cursors key it writes, so it owns the reader too.
    `ctx["metrics_sync"]` overrides (a caller that already read it)."""
    if isinstance(ctx, dict) and "metrics_sync" in ctx:
        return ctx.get("metrics_sync") or {}
    try:
        import scheduler
        return scheduler.metrics_sync_state(_rid(r), conn=conn) or {}
    except Exception as e:
        print(f"[data_freshness] metrics sync state unreadable for {_rid(r)}: {e}")
        return None


def _visibility(r, conn, today, now, ctx, db_path=None):
    try:
        row = conn.execute("SELECT MAX(created_at) AS t FROM ai_visibility_runs WHERE restaurant_id=?",
                           (_rid(r),)).fetchone()
    except Exception:
        row = None
    d = _as_date(row["t"] if row else None)
    if d is None:
        return _result("visibility", None, None, "AI visibility not checked yet", state="not_connected")
    return _data_date_state("visibility", d, today, "AI visibility checked")


def _competitor(r, conn, today, now, ctx, db_path=None):
    d = _as_date(_get(r, "competitor_updated_at"))
    if d is None:
        return _result("competitor", None, None, "No competitor read yet", state="not_connected")
    return _data_date_state("competitor", d, today, "Competitors read")


def _weather(r, conn, today, now, ctx, db_path=None):
    """The cached forecast's age in hours, read by weather.py's own rule
    (G11: weather_cached_at is UTC with an offset now, server-local naive
    on older rows) and its own line — `stale` past
    weather.FORECAST_STALE_HOURS, or when the age is unknown. A stale copy
    is an error on this source (under ERROR_CEILING), so Home never calls a
    72-hour-old fallback forecast current."""
    raw = _get(r, "weather_cached_at")
    if not raw:
        return _result("weather", None, None, "No forecast cached", state="not_connected")
    import weather
    at = _stamp(raw, naive_tz="local")
    if at is not None and (at - now).total_seconds() > 3600:
        at = None           # cached "in the future": its age is unknown
    age_h = max(0.0, (now - at).total_seconds() / 3600.0) if at is not None else None
    stale = age_h is None or age_h > weather.FORECAST_STALE_HOURS
    if at is None:
        return _result("weather", 0, None, "Weather: forecast age unknown", state="unknown",
                       error="forecast age unknown", age_hours=None, stale=True)
    err = f"Forecast is {int(age_h)} hours old" if stale else None
    iso = at.date().isoformat()
    pct = age_pct("weather", age_h / 24.0, 1.0, bool(err))
    basis = f"Forecast cached {ce._mdy(iso)}" + (f" · {err}" if err else "")
    return _result("weather", pct, iso, basis, error=err, age_hours=round(age_h, 1), stale=stale,
                   lag_days=round(age_h / 24.0, 2))


# A provisional daily report (sales still missing when it went out) is recent
# but incomplete: it can date the source, never above "aging".
DSR_PROVISIONAL_CAP = ce.CURRENT_AT - 1


def _dsr(r, conn, today, now, ctx, db_path=None):
    """The latest daily report. A FINAL report dates it as any source; a
    provisional one (set_stage stamps it like a final one, and it read 100%
    current — re-audit B3#12) is held under DSR_PROVISIONAL_CAP, so a
    provisional night is never "current". The better of the two stands, and
    a newer provisional night is named in the basis."""
    try:
        row = conn.execute("SELECT MAX(business_date) AS d FROM dsr_reports WHERE restaurant_id=? "
                           "AND finalized_at IS NOT NULL AND COALESCE(provisional, 0) = 0", (_rid(r),)).fetchone()
        prov = conn.execute("SELECT MAX(business_date) AS d FROM dsr_reports WHERE restaurant_id=? "
                            "AND finalized_at IS NOT NULL AND COALESCE(provisional, 0) = 1", (_rid(r),)).fetchone()
    except Exception:
        row, prov = None, None
    d = _as_date(row["d"] if row else None)
    p = _as_date(prov["d"] if prov else None)
    if p is not None and d is not None and p <= d:
        p = None                          # an older provisional night changes nothing
    if d is None and p is None:
        return _result("dsr", None, None, "No daily report yet", state="not_connected")
    final = _data_date_state("dsr", d, today, "Daily report for") if d is not None else None
    if p is None:
        return final
    pv = _data_date_state("dsr", p, today, "Daily report for", 1.0, "provisional — sales were missing")
    if pv.get("pct") is not None and pv.get("state") != "unknown":
        pv["pct"] = min(pv["pct"], DSR_PROVISIONAL_CAP)
        pv["state"] = ce.state(pv["pct"])
    pv["provisional"] = True
    if final is not None and (final.get("pct") or 0) >= (pv.get("pct") or 0):
        final["basis"] += f" · the report for {ce._mdy(p.isoformat())} is provisional"
        return final
    return pv


_READERS = {"pos": _pos, "labor": _labor, "sales": _sales, "reviews": _reviews, "inventory": _inventory,
            "purchases": _purchases, "marketing": _marketing, "visibility": _visibility,
            "competitor": _competitor, "weather": _weather, "dsr": _dsr}


def source_state(restaurant, key, db_path=None, now=None, context=None) -> dict:
    """{key, label, pct, as_of, as_of_iso, basis, state, error} for one
    source. state: current | aging | stale (from pct, confidence_engine.state)
    | not_connected (pct None — the source does not apply) | unknown (no
    usable date: pct 0, never fresh — or pct None for a POS that has never
    synced, which no figure rests on yet). `error` is set whenever the
    source reports one, and an erroring source's pct is under ERROR_CEILING
    (strictly below confidence_engine.STALE_BELOW). Never raises."""
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
        return _READERS[key](restaurant, conn, _today(restaurant, now), now, context, db_path)
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
