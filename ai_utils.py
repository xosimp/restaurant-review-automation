"""
ai_utils.py — shared helpers for Claude API calls across the app.

Every module (analyser, drafter, competitor, labor, marketing, inventory,
reporter, client_api) was calling client.messages.create() directly with no
retry logic — a transient rate limit or timeout just silently dropped that
one item (a review never got analyzed, a draft never got written), with no
backoff and no second attempt. This wraps the call once so every caller gets
the same retry behavior instead of each reimplementing it inconsistently.

Since fix round G (9/29/26) it is also the AI operations ledger: every call —
including one refused before it reached a provider (budget, breaker,
readiness gate, missing key) — is a row in ai_usage with its vendor, outcome,
stop reason, attempts, total latency, request id, trigger, actor and
correlation id; each provider call leaves a trace in ai_calls; guard and
validation findings land in ai_quality_events (not job_failures); breaker
transitions, budget stops and credential failures land in ai_health_events
and page the operator; ai_usage_daily keeps the history the 120-day prune
used to delete. Google Places has its own ceiling and one metered,
budget-checked request helper (places_request).
"""
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
import zlib
import anthropic

log = logging.getLogger("ai_utils")


def _conn(db_path=None):
    """A connection to `db_path`, else the default database — models.get_conn
    and models.DB_PATH resolved at call time (CLAUDE.md's bound-import
    hazard: a test's patch of either must reach every writer here)."""
    import models
    return models.get_conn(db_path or models.DB_PATH)

# Errors worth retrying — transient/server-side. NOT retried: BadRequestError,
# AuthenticationError, PermissionDeniedError, NotFoundError — those are
# caller mistakes or config problems that a retry will never fix.
#
# A 529 (overloaded) is NOT an InternalServerError in the SDK — it is its own
# OverloadedError, a sibling under APIStatusError — so it fell through to the
# non-retryable branch: no retry, no breaker, no capture (#104). _is_retryable
# decides by status code as well as by class, so a 529, 503 or 504 from any
# SDK version is retried and, once the retries are spent, counted by the
# breaker.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)
_RETRYABLE_STATUS = (408, 409, 429, 500, 502, 503, 504, 529)


# ── spend budget ────────────────────────────────────────────────────────────
#
# ai_rate_limited() below caps bursts — six calls a minute stops a client
# mashing "Regenerate". It says nothing about the month: six a minute, all
# day, every day is a bill nobody notices until the card is charged, and a
# loop in a scheduled job (or a client scripting the API) has no ceiling at
# all. These are the ceilings, checked at the one place every Claude call in
# the app passes through.
#
# Defaults are generous against real usage (a full-tier restaurant runs well
# under a dollar a day) — they exist to stop a runaway, not to ration normal
# work. Raise them in Railway env vars, or set a budget to 0 to disable that
# ceiling entirely.
AI_DAILY_BUDGET_USD = float(os.getenv("AI_DAILY_BUDGET_USD", "10"))
AI_MONTHLY_BUDGET_USD = float(os.getenv("AI_MONTHLY_BUDGET_USD", "150"))
# The real backstop: total spend across every restaurant, so one bad deploy
# can't drain the account through a hundred separate under-budget clients.
#
# It SCALES with the number of paying clients. As a flat $1,500 it was ten
# clients at their own monthly cap before the shared pool bound — and the
# blast radius got worse with every client won, because one runaway would
# refuse AI for everyone. The floor keeps the backstop meaningful at one or
# two clients; the per-client allowance means the ceiling grows with the
# business instead of throttling it.
AI_GLOBAL_MONTHLY_BUDGET_USD = float(os.getenv("AI_GLOBAL_MONTHLY_BUDGET_USD", "1500"))
AI_GLOBAL_PER_CLIENT_USD = float(os.getenv("AI_GLOBAL_PER_CLIENT_USD", "200"))
# ...but never above this. A backstop that grows without limit stops being a
# backstop (AI-12): at a few hundred clients a runaway loop could spend tens
# of thousands of dollars before the "shared ceiling" noticed. Raising it is
# a deliberate Railway variable, not a side effect of signing clients.
AI_GLOBAL_MAX_MONTHLY_BUDGET_USD = float(os.getenv("AI_GLOBAL_MAX_MONTHLY_BUDGET_USD", "10000"))


def _paying_client_count(db_path=None):
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) c FROM restaurants "
            f"WHERE LOWER(TRIM(COALESCE(billing_status,''))) IN {_PAID_STATES_SQL}"
        ).fetchone()["c"]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def global_monthly_budget(db_path=None):
    """The shared ceiling for this many paying clients.

    Never below AI_GLOBAL_MONTHLY_BUDGET_USD, so a small client base still has
    a real backstop; above that it is AI_GLOBAL_PER_CLIENT_USD per paying
    client, so winning a client raises the pool rather than shrinking
    everyone's share of it — up to AI_GLOBAL_MAX_MONTHLY_BUDGET_USD, the
    absolute ceiling. A budget of 0 still disables the ceiling.
    """
    if not AI_GLOBAL_MONTHLY_BUDGET_USD:
        return 0.0
    scaled = max(AI_GLOBAL_MONTHLY_BUDGET_USD,
                 _paying_client_count(db_path) * AI_GLOBAL_PER_CLIENT_USD)
    if AI_GLOBAL_MAX_MONTHLY_BUDGET_USD:
        scaled = min(scaled, max(AI_GLOBAL_MAX_MONTHLY_BUDGET_USD, AI_GLOBAL_MONTHLY_BUDGET_USD))
    return scaled

# Unpaid accounts — demos, prospects, anything not billing_status active,
# past_due or internal — get their own, much smaller ceilings, AND their
# spend is excluded from the global figure above.
#
# Audit #5 found the sharp edge: ai_budget_exceeded checks the global ceiling
# first, for every caller, so spend on non-paying accounts could exhaust it
# and the people who then saw "AI is over budget" were the paying clients.
# A demo account going stale, or a handful of prospect accounts left open,
# should never be able to take Ask Cavnar away from someone who pays for it.
AI_UNPAID_DAILY_BUDGET_USD = float(os.getenv("AI_UNPAID_DAILY_BUDGET_USD", "2"))
AI_UNPAID_MONTHLY_BUDGET_USD = float(os.getenv("AI_UNPAID_MONTHLY_BUDGET_USD", "25"))

# Trial accounts (billing_status 'trial', not a demo) — a prospect evaluating
# the product (owner decision 2, 9/29/26). They were on the $2/day demo
# ceiling, so a prospect generating two schedules on day one was told "AI is
# paused" before lunch (#122). A trial is a sale in progress: its ceiling is
# its own, deliberately, and like the unpaid ceilings its spend stays out of
# the global pool paying clients share.
AI_TRIAL_DAILY_BUDGET_USD = float(os.getenv("AI_TRIAL_DAILY_BUDGET_USD", "5"))
AI_TRIAL_MONTHLY_BUDGET_USD = float(os.getenv("AI_TRIAL_MONTHLY_BUDGET_USD", "50"))

# Every trial together (owner decision, 9/29/26). The ceilings above bound
# one trial; nothing bounded how many there are, so a provisioning bug, a
# scripted signup the day ALLOW_PUBLIC_SIGNUP is set, or a loop across every
# trial multiplied them. All trials share this second ceiling — apart from
# the paying pool, which they never draw on, so trials can never take AI
# from a paying client either. Sized at ten trials at their own ceilings, so
# normal use never meets it; 80% pages Will. 0 disables it.
AI_TRIAL_POOL_DAILY_USD = float(os.getenv("AI_TRIAL_POOL_DAILY_USD", "50"))
AI_TRIAL_POOL_MONTHLY_USD = float(os.getenv("AI_TRIAL_POOL_MONTHLY_USD", "500"))

# Google Places is a data API, not AI, and has its own per-restaurant ceiling
# (owner decision 2). Its spend used to draw down the AI ceiling — a geocode
# loop could pause a trial's Ask and reply drafts while the Places calls
# carried on, because nothing ever refused a Places call (#122, #123). A
# normal restaurant spends about $0.07 a day (four review fetches) and a few
# dollars a month; a busy day with competitor refreshes stays under $1. The
# ceiling stops a loop like the 234-calls-a-day geocode one long before it
# costs anything that matters. 0 disables that ceiling.
AI_PLACES_DAILY_BUDGET_USD = float(os.getenv("AI_PLACES_DAILY_BUDGET_USD", "3"))
AI_PLACES_MONTHLY_BUDGET_USD = float(os.getenv("AI_PLACES_MONTHLY_BUDGET_USD", "30"))

# Spend past this share of a ceiling is a warning: an issue on the console
# and, for the global pool, a page — before the ceiling stops anything.
AI_BUDGET_WARN_PCT = float(os.getenv("AI_BUDGET_WARN_PCT", "80"))

# billing_status values that count as paying for budget purposes:
# models.PAYING_BILLING_STATES — active, and past due while Stripe retries
# the card (owner decision, 9/29/26: a past-due client is still a contracted,
# in-service customer, and pausing their AI mid-dunning is the wrong moment
# to degrade the product) — plus internal. Kept literal because ai_utils
# never imports models at module level; tests/test_fix_integration_lead.py
# holds the two equal. One list for the tier, the pool's spend and the
# pool's client count: past-due spend draws on the pool AND is counted in it.
_PAID_BILLING_STATES = frozenset({"active", "past_due", "internal"})
_PAID_STATES_SQL = "(" + ",".join("'%s'" % s for s in sorted(_PAID_BILLING_STATES)) + ")"
_TRIAL_BILLING_STATES = frozenset({"trial"})
_TRIAL_STATES_SQL = "(" + ",".join("'%s'" % s for s in sorted(_TRIAL_BILLING_STATES)) + ")"
# The trial pool's scopes in a budget status, and the words a refusal uses.
# _record_budget_stop and _note_budget_warnings page on these.
_TRIAL_POOL_SCOPES = {"trial_pool_day": "daily budget shared by all trial accounts",
                      "trial_pool_month": "monthly budget shared by all trial accounts"}

# Budget tiers: which ceilings a restaurant's AI spend answers to.
TIER_PAID, TIER_TRIAL, TIER_UNPAID = "paid", "trial", "unpaid"


def _budget_tier(restaurant_id, db_path=None):
    """'paid', 'trial' or 'unpaid' — which AI ceilings this restaurant has.

    A row that does not exist is not a client: unpaid. A demo account is
    unpaid whatever its billing_status says (demos are created as 'trial').
    A lookup that ERRORS fails open to paid, matching what every other gate in
    this codebase does (subscription_allows_access, restaurant_has_module): a
    database hiccup must not quietly drop a paying client onto a $2 ceiling.
    restaurant_id always comes from an authenticated session, so there is no
    caller who benefits from the open direction."""
    if restaurant_id is None:
        return TIER_PAID   # global/system calls are not attributable to a client
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        try:
            try:
                row = conn.execute("SELECT billing_status, COALESCE(is_demo, 0) AS is_demo "
                                   "FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute("SELECT billing_status, 0 AS is_demo FROM restaurants WHERE id=?",
                                   (restaurant_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return TIER_UNPAID
        status = (row["billing_status"] or "").strip().lower()
        if status in _PAID_BILLING_STATES:
            return TIER_PAID
        if status in _TRIAL_BILLING_STATES and not row["is_demo"]:
            return TIER_TRIAL
        return TIER_UNPAID
    except Exception:
        return TIER_PAID


def _is_paid_account(restaurant_id, db_path=None):
    """Whether this restaurant draws on the paid budgets and the global pool."""
    return _budget_tier(restaurant_id, db_path) == TIER_PAID


def _tier_budgets(tier):
    """(daily, monthly) AI ceilings for a budget tier."""
    if tier == TIER_PAID:
        return AI_DAILY_BUDGET_USD, AI_MONTHLY_BUDGET_USD
    if tier == TIER_TRIAL:
        return AI_TRIAL_DAILY_BUDGET_USD, AI_TRIAL_MONTHLY_BUDGET_USD
    return AI_UNPAID_DAILY_BUDGET_USD, AI_UNPAID_MONTHLY_BUDGET_USD


_TIER_LABELS = {
    TIER_PAID: ("daily budget", "monthly budget"),
    TIER_TRIAL: ("daily budget for trial accounts", "monthly budget for trial accounts"),
    TIER_UNPAID: ("daily budget for accounts that aren't on a paid plan",
                  "monthly budget for accounts that aren't on a paid plan"),
}

# Spend only moves when a call completes, and a SUM over ai_usage on every
# call would be pure overhead on a path that already takes seconds.
_BUDGET_CACHE_SECS = 60
_budget_cache = {}


class AIBudgetExceeded(RuntimeError):
    """Raised instead of making a Claude call that would exceed a budget.

    Its own type so callers can tell "we stopped spending" apart from "the
    AI is down" — they mean very different things to whoever sees the
    message.
    """


# Which ledger rows a budget scope sums. Places is its own vendor with its
# own ceiling (owner decision 2): it never draws down the AI budget again.
# `model` is checked as well as `vendor` so a row written by an older process
# during a deploy (vendor still NULL) is classed the same way.
_PLACES_ROW_SQL = "(vendor='google_places' OR COALESCE(model,'') LIKE 'google-places%')"
_AI_ROW_SQL = "(COALESCE(vendor,'') <> 'google_places' AND COALESCE(model,'') NOT LIKE 'google-places%')"


def _spend_since(sql_window, restaurant_id=None, db_path=None, paid_only=False, scope="ai", trials_only=False):
    """Spend since `sql_window` for one budget scope.

    scope "ai" — Claude and Perplexity. A restaurant's own figure leaves out
    calls an admin triggered (#148: a seeded draft or a menu extraction the
    operator ran is the operator's spend, not the client's ceiling); the
    global pool counts them, so they are never unbounded.
    scope "places" — Google Places only.
    trials_only — every trial account's own spend together (not a demo's,
    not an admin's run): the trial pool."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    try:
        _ensure_usage_schema(conn, path)
        where = "WHERE created_at >= ? AND " + (_PLACES_ROW_SQL if scope == "places" else _AI_ROW_SQL)
        params = [sql_window]
        if restaurant_id is not None:
            where += " AND restaurant_id=?"
            params.append(restaurant_id)
            if scope != "places":
                where += " AND COALESCE(\"trigger\",'') <> 'admin'"
        elif paid_only:
            # The global pool is what paying clients share. Spend on demo and
            # prospect accounts is bounded by its own ceilings and must not
            # count here, or those accounts can starve the people paying —
            # except what an admin ran, which no client ceiling counts.
            where += (" AND (restaurant_id IS NULL OR COALESCE(\"trigger\",'') = 'admin' OR restaurant_id IN "
                      "(SELECT id FROM restaurants WHERE LOWER(TRIM(COALESCE(billing_status,''))) IN "
                      + _PAID_STATES_SQL + "))")
        elif trials_only:
            where += (" AND COALESCE(\"trigger\",'') <> 'admin' AND restaurant_id IN "
                      "(SELECT id FROM restaurants WHERE LOWER(TRIM(COALESCE(billing_status,''))) IN "
                      + _TRIAL_STATES_SQL + " AND COALESCE(is_demo, 0) = 0)")
        sql = f"SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM ai_usage {where}"
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            # The table or a column went missing since this process last
            # checked — a deploy migrating the volume under a live instance,
            # a restored backup. This runs before EVERY Claude call, so
            # raising here would refuse all AI rather than report spend.
            _ensure_usage_schema(conn, path, force=True)
            row = conn.execute(sql, params).fetchone()
        return float(row["spend"] or 0.0)
    finally:
        conn.close()


def _current_windows():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d 00:00:00"), now.strftime("%Y-%m-01 00:00:00")


def _prune_budget_cache(current_windows):
    """Drop every cached total whose window is not a current one (AI-10).

    Keys are (scope, window-start); yesterday's day key is never read again,
    but nothing removed it, so the dict grew by one key per restaurant per day
    for the life of the process — and note_ai_spend walks the whole dict on
    every AI call."""
    for key in list(_budget_cache):
        if key[1] not in current_windows:
            _budget_cache.pop(key, None)


def _cached_spend(cache_key, sql_window, restaurant_id, db_path, paid_only=False, scope="ai", trials_only=False):
    now = time.time()
    hit = _budget_cache.get(cache_key)
    if hit and now - hit[0] < _BUDGET_CACHE_SECS:
        return hit[1]
    spend = _spend_since(sql_window, restaurant_id, db_path, paid_only=paid_only, scope=scope,
                         trials_only=trials_only)
    _budget_cache[cache_key] = (now, spend)
    return spend


def _places_key(restaurant_id):
    """The budget-cache scope for one restaurant's Places spend. A 2-tuple key
    (scope, window) like every other, so _prune_budget_cache reads it."""
    return f"places:{restaurant_id}"


def _resets_at(scope_window):
    """When a day or month window ends (UTC, ISO) — "paused until"."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    if scope_window == "day":
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1)
    return nxt.isoformat(timespec="seconds")


def _finish_status(out):
    for k, v in out.items():
        if not isinstance(v, dict) or "budget" not in v:
            continue
        v["over"] = bool(v["budget"]) and v["spend"] >= v["budget"]
        v["pct"] = round((v["spend"] / v["budget"]) * 100, 1) if v["budget"] else 0.0
        # The warning line (#122): an issue before the ceiling stops anything.
        v["warn"] = bool(v["budget"]) and v["pct"] >= AI_BUDGET_WARN_PCT
    return out


def _trial_pool_status(day, month, db_path):
    """Every trial's spend together against the trial pool (trial_pool_day /
    trial_pool_month)."""
    return {"trial_pool_day": {"spend": _cached_spend(("trial_pool", day), day, None, db_path, trials_only=True),
                               "budget": AI_TRIAL_POOL_DAILY_USD, "resets_at": _resets_at("day")},
            "trial_pool_month": {"spend": _cached_spend(("trial_pool", month), month, None, db_path,
                                                        trials_only=True),
                                 "budget": AI_TRIAL_POOL_MONTHLY_USD, "resets_at": _resets_at("month")}}


def ai_budget_status(restaurant_id=None, db_path=None):
    """Spend against each AI ceiling (Claude and Perplexity; Places has its
    own, places_budget_status). Also what the admin console reads.

    With a restaurant: `tier` is paid / trial / unpaid, `paid` stays for the
    callers that read it, and each window carries spend, budget, pct, warn
    (≥ AI_BUDGET_WARN_PCT), over and resets_at. A trial also carries the
    trial pool (trial_pool_day / trial_pool_month); so does the platform
    view (no restaurant), for the console."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d 00:00:00")
    month = now.strftime("%Y-%m-01 00:00:00")
    _prune_budget_cache((day, month))
    out = {
        "global_month": {"spend": _cached_spend(("g", month), month, None, db_path, paid_only=True),
                         "budget": global_monthly_budget(db_path), "resets_at": _resets_at("month")},
    }
    if restaurant_id is not None:
        # An unpaid or trial account is bounded by its own, smaller ceilings
        # and is deliberately absent from the global figure above, so it
        # cannot exhaust the pool a paying client depends on.
        tier = _budget_tier(restaurant_id, db_path)
        daily, monthly = _tier_budgets(tier)
        out["day"] = {"spend": _cached_spend((restaurant_id, day), day, restaurant_id, db_path),
                      "budget": daily, "resets_at": _resets_at("day")}
        out["month"] = {"spend": _cached_spend((restaurant_id, month), month, restaurant_id, db_path),
                        "budget": monthly, "resets_at": _resets_at("month")}
        out["tier"] = tier
        out["paid"] = tier == TIER_PAID
        if tier != TIER_PAID:
            # An account off the paid plan is never refused for the global
            # pool it does not draw on.
            out["global_month"] = dict(out["global_month"], budget=0.0)
        if tier == TIER_TRIAL:
            out.update(_trial_pool_status(day, month, db_path))
    else:
        out.update(_trial_pool_status(day, month, db_path))
    return _finish_status(out)


def places_budget_status(restaurant_id=None, db_path=None):
    """One restaurant's Google Places spend against its own ceilings, or,
    with no restaurant, the platform's Places spend (no ceiling — a fleet
    total, for the console)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d 00:00:00")
    month = now.strftime("%Y-%m-01 00:00:00")
    _prune_budget_cache((day, month))
    if restaurant_id is None:
        return _finish_status({"month": {"spend": _cached_spend(("places:all", month), month, None, db_path,
                                                                scope="places"),
                                         "budget": 0.0, "resets_at": _resets_at("month")}})
    key = _places_key(restaurant_id)
    return _finish_status({
        "day": {"spend": _cached_spend((key, day), day, restaurant_id, db_path, scope="places"),
                "budget": AI_PLACES_DAILY_BUDGET_USD, "resets_at": _resets_at("day")},
        "month": {"spend": _cached_spend((key, month), month, restaurant_id, db_path, scope="places"),
                  "budget": AI_PLACES_MONTHLY_BUDGET_USD, "resets_at": _resets_at("month")},
    })


def places_budget_exceeded(restaurant_id=None, db_path=None):
    """Which Places ceiling this restaurant has reached, as words, or None.
    Unattributed requests (no restaurant) answer to no per-restaurant
    ceiling. Fails open, like ai_budget_exceeded."""
    if restaurant_id is None:
        return None
    try:
        st = places_budget_status(restaurant_id, db_path)
        for scope, label in (("day", "daily Google Places budget"), ("month", "monthly Google Places budget")):
            if st.get(scope, {}).get("over"):
                return label
        _note_budget_warnings(st, restaurant_id, vendor="google_places")
        return None
    except Exception as e:
        log.warning("places budget check failed (rid=%s): %s", restaurant_id, e)
        return None


def ai_budget_exceeded(restaurant_id=None, db_path=None, trigger=None):
    """Which ceiling is blown, as a human-readable scope, or None.

    A call an admin triggered answers only to the global pool: the client's
    own ceiling is for the client's own use (#148). `trigger` defaults to the
    current attribution (ai_context / the request / the stack).

    Fails OPEN: if the ledger can't be read, the call goes through. A budget
    check is a backstop against a runaway, and letting a broken query take
    every AI feature offline would be a worse outage than the overspend it's
    guarding against — the failure is captured so it doesn't stay invisible.
    """
    try:
        if trigger is None:
            trigger = _attribution()[0]
        status = ai_budget_status(restaurant_id, db_path)
        day_label, month_label = _TIER_LABELS.get(status.get("tier") or TIER_PAID, _TIER_LABELS[TIER_PAID])
        scopes = [("global_month", "monthly budget across all clients")]
        if trigger != "admin":
            scopes += [("day", day_label), ("month", month_label)]
            if restaurant_id is not None and status.get("tier") == TIER_TRIAL:
                scopes += list(_TRIAL_POOL_SCOPES.items())
        for scope, label in scopes:
            entry = status.get(scope)
            if isinstance(entry, dict) and entry.get("over"):
                return label
        _note_budget_warnings(status, restaurant_id, vendor="anthropic")
        return None
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="ai_budget_check", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return None


def _budget_period(scope, restaurant_id):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    window = f"{now:%Y-%m}" if ("month" in scope or scope == "global") else f"{now:%Y-%m-%d}"
    return f"{scope}:{restaurant_id}:{window}"


def _record_budget_stop(scope, restaurant_id, vendor="anthropic"):
    """One health event per scope per restaurant per day, not one per blocked
    call — and a page when the stop is the global pool, which stops AI for
    every client. It used to be a job_failures row ("Job `ai_budget`
    failed"), which paged nobody (#104)."""
    try:
        import ops
        from datetime import datetime, timezone
        period = f"{vendor}:{scope}:{restaurant_id}:{datetime.now(timezone.utc):%Y-%m-%d}"
        if not ops.claim_period("ai_budget_stop", period):
            return
    except Exception as e:
        log.warning("budget stop claim failed: %s", e)
        return
    record_health_event(vendor, "budget_stop", scope=scope, restaurant_id=restaurant_id,
                        detail=f"{scope} reached")
    if scope in _TRIAL_POOL_SCOPES.values():
        _page(f"budget_stop:trial_pool:{vendor}", "Cavnar AI: AI is paused for every trial account (budget)",
              [f"The {scope} was reached — every trial's AI calls are refused until it resets or the "
               f"ceiling is raised (AI_TRIAL_POOL_DAILY_USD / AI_TRIAL_POOL_MONTHLY_USD).",
               "Paying clients are not affected. If these are real trials, raise the ceiling; if not, "
               "look at who created them. Open the admin console → Operations → AI to see what spent it."])
    if "across all clients" in str(scope):
        _page(f"budget_stop:{vendor}", "Cavnar AI: AI is paused for every client (budget)",
              [f"The {scope} was reached — every AI call is refused until it resets or the "
               f"ceiling is raised (AI_GLOBAL_MONTHLY_BUDGET_USD / AI_GLOBAL_MAX_MONTHLY_BUDGET_USD).",
               "Open the admin console → Operations → AI to see what spent it."])


_warned_memo = set()
_WARNED_MEMO_MAX = 5000


def _note_budget_warnings(status, restaurant_id, vendor="anthropic"):
    """Record (and for the global pool, page) spend past AI_BUDGET_WARN_PCT of
    a ceiling, once per scope per window. Cheap: nothing is read or written
    until a window is actually past the line, and a window already claimed in
    this process is skipped without touching the database."""
    for scope, entry in (status or {}).items():
        if not isinstance(entry, dict) or not entry.get("warn") or entry.get("over"):
            continue
        platform = scope == "global_month" or scope in _TRIAL_POOL_SCOPES
        key = "global" if scope == "global_month" else f"{scope}"
        period = _budget_period(key, None if platform else restaurant_id)
        memo = (vendor, period)
        if memo in _warned_memo:
            continue
        if len(_warned_memo) >= _WARNED_MEMO_MAX:
            _warned_memo.clear()
        _warned_memo.add(memo)
        try:
            import ops
            if not ops.claim_period(f"ai_budget_warn:{vendor}", period):
                continue
        except Exception:
            continue
        detail = f"{scope} at {entry.get('pct')}% (${entry.get('spend', 0):.2f} of ${entry.get('budget', 0):.2f})"
        record_health_event(vendor, "budget_warn", scope=scope,
                            restaurant_id=None if platform else restaurant_id, detail=detail)
        if scope in _TRIAL_POOL_SCOPES:
            _page(f"budget_warn:trial_pool:{vendor}", "Cavnar AI: trial accounts' combined AI spend is past "
                                                     f"{int(AI_BUDGET_WARN_PCT)}%",
                  [f"All trial accounts together: {detail}.",
                   "At 100% every trial's AI calls are refused until it resets; paying clients are not "
                   "affected. Raise AI_TRIAL_POOL_DAILY_USD / AI_TRIAL_POOL_MONTHLY_USD if the trials are real."])
        if scope == "global_month":
            _page(f"budget_warn:{vendor}", "Cavnar AI: the platform AI budget is past "
                                          f"{int(AI_BUDGET_WARN_PCT)}%",
                  [f"Monthly spend across all paying clients is {detail}.",
                   "At 100% every AI call is refused until the month resets."])


def note_ai_spend(cost_usd, restaurant_id=None, vendor=None, trigger=None):
    """Keep the cached totals honest between refreshes, so a burst inside one
    cache window still trips the ceiling. Places spend moves the Places
    totals only; an admin-triggered call moves the global pool only."""
    if not cost_usd:
        return
    _prune_budget_cache(_current_windows())
    if vendor == "google_places":
        scopes = (_places_key(restaurant_id), "places:all")
    elif trigger == "admin":
        scopes = ("g",)
    else:
        scopes = ("g", restaurant_id)
        # A trial's spend moves the trial pool too — looked up only while a
        # pool total is cached, so a paying client's call never pays for it.
        if (restaurant_id is not None and any(k[0] == "trial_pool" for k in list(_budget_cache))
                and _budget_tier(restaurant_id) == TIER_TRIAL):
            scopes += ("trial_pool",)
    for key in list(_budget_cache):
        if key[0] in scopes:
            hit = _budget_cache.get(key)
            if hit:
                _budget_cache[key] = (hit[0], hit[1] + cost_usd)


# ── circuit breaker ─────────────────────────────────────────────────────────
#
# Retry is the right answer to ONE failed call and the wrong answer to a
# provider outage. With retries=2 and a 1.5^n backoff, every call during a
# Claude outage costs three attempts and ~4 seconds of held thread before it
# fails — on a deployment with four request threads, and with a scheduler
# pass that makes one call per new review across every restaurant. The
# retries do not help (the provider is down) and the waiting is what turns
# somebody else's outage into ours.
#
# So: after CB_FAILURE_THRESHOLD consecutive exhausted-retry failures, stop
# calling for CB_OPEN_SECONDS and fail immediately with a message that says
# what is actually happening. One probe is allowed through when the window
# expires; a success closes the breaker.
#
# Process-local on purpose. It must be readable on the hot path without a
# database round trip, and the state it protects is this process's threads.
# A budget stop is NOT a provider failure and never trips it.
CB_FAILURE_THRESHOLD = int(os.getenv("AI_BREAKER_THRESHOLD", "5"))
CB_OPEN_SECONDS = int(os.getenv("AI_BREAKER_OPEN_SECONDS", "60"))

_breakers = {}
_breaker_lock = threading.Lock()


class AIProviderDown(RuntimeError):
    """Raised instead of calling a provider that just failed repeatedly."""


# How long one probe may hold the half-open breaker before another caller is
# allowed to try. A probe whose thread died without reporting must not keep
# the breaker shut forever; a healthy probe reports within its own bounded
# call (DEFAULT_AI_TIMEOUT per attempt).
CB_PROBE_SECONDS = int(os.getenv("AI_BREAKER_PROBE_SECONDS", "120"))

_PROVIDER_DOWN_MESSAGE = ("Cavnar AI's model provider is not responding right now. Nothing is lost — "
                          "try again in a minute.")


# The providers that have a breaker. Perplexity and Places had none (#151):
# a revoked Perplexity key held an owner's request thread for 20+ seconds on
# every Intel refresh, each query paced and retried against a key that could
# never work.
BREAKER_PROVIDERS = ("anthropic", "perplexity", "google_places")
VENDOR_LABELS = {"anthropic": "Claude (Anthropic)", "perplexity": "Perplexity", "google_places": "Google Places"}

_PLACES_DOWN_MESSAGE = "Google Places isn't answering right now — try again in a minute."


def breaker_state(provider="anthropic"):
    """(state, seconds_remaining) — "closed", "open" or "probing"."""
    with _breaker_lock:
        b = _breakers.get(provider)
        if b and b.get("probe_until", 0) > time.time():
            return "probing", 0
        if not b or not b.get("open_until"):
            return "closed", 0
        remaining = b["open_until"] - time.time()
        if remaining <= 0:
            return "probing", 0
        return "open", round(remaining, 1)


def breakers():
    """Every provider's breaker in this process: {provider: {state,
    seconds_remaining, failures, opened_at, reason}}."""
    out = {}
    for p in BREAKER_PROVIDERS:
        state, remaining = breaker_state(p)
        with _breaker_lock:
            b = dict(_breakers.get(p) or {})
        out[p] = {"state": state, "seconds_remaining": remaining, "failures": int(b.get("failures") or 0),
                  "opened_at": b.get("opened_at"), "reason": b.get("reason")}
    return out


def breaker_open(provider):
    """True while `provider` is refusing calls (open, or a probe in flight) —
    the fail-fast check for a caller that is not about to call itself."""
    return breaker_state(provider)[0] != "closed"


def _breaker_check(provider):
    """Raise if the breaker is open. Lets exactly one probe through after.

    Single-flight (AI-13): the first caller after the window expires becomes
    the probe and marks the breaker half-open; every other caller is refused
    until that probe reports (or CB_PROBE_SECONDS pass without a report).
    Clearing open_until alone let every concurrent caller through as "the
    probe", so a provider that was still down took the whole burst again."""
    now = time.time()
    with _breaker_lock:
        b = _breakers.get(provider)
        if not b:
            return
        if b.get("probe_until", 0) > now:
            raise AIProviderDown(_PROVIDER_DOWN_MESSAGE)
        if not b.get("open_until"):
            return
        if now < b["open_until"]:
            raise AIProviderDown(_PROVIDER_DOWN_MESSAGE)
        # Window expired: allow this one call through as the probe.
        b["open_until"] = 0.0
        b["failures"] = CB_FAILURE_THRESHOLD - 1
        b["probe_until"] = now + CB_PROBE_SECONDS


def _breaker_release_probe(provider):
    """The call ended without a verdict on the provider's health (a 400, a
    malformed request) — the provider answered, so stop refusing others."""
    with _breaker_lock:
        b = _breakers.get(provider)
        if b:
            b["probe_until"] = 0.0


def _breaker_record(provider, ok, reason=None):
    """Count a call's verdict on the provider's health. A transition — the
    breaker opening, or closing again after it had opened — is recorded in
    ai_health_events and an opening pages the operator (#104). The side
    effects run outside the lock: they write to the database."""
    transition = None
    with _breaker_lock:
        b = _breakers.setdefault(provider, {"failures": 0, "open_until": 0.0})
        b["probe_until"] = 0.0
        if ok:
            if b.get("opened_at"):
                transition = ("close", b.get("failures") or 0, b.get("reason"))
                b["opened_at"] = None
            b["failures"] = 0
            b["open_until"] = 0.0
            b["reason"] = None
        else:
            b["failures"] += 1
            if reason:
                b["reason"] = reason
            if b["failures"] >= CB_FAILURE_THRESHOLD:
                was_open = bool(b.get("opened_at"))
                b["open_until"] = time.time() + CB_OPEN_SECONDS
                if not was_open:
                    b["opened_at"] = time.time()
                    transition = ("open", b["failures"], b.get("reason"))
    if transition:
        _note_breaker_transition(provider, *transition)


def trip_breaker(provider, reason):
    """Open `provider`'s breaker now, without waiting for the threshold — a
    revoked key or exhausted credit fails every call the same way, so the
    first 401/403 is enough (#151). A probe is still let through when the
    window expires, so a fixed key closes it on the next call."""
    transition = None
    with _breaker_lock:
        b = _breakers.setdefault(provider, {"failures": 0, "open_until": 0.0})
        b["probe_until"] = 0.0
        b["failures"] = max(int(b.get("failures") or 0), CB_FAILURE_THRESHOLD)
        b["open_until"] = time.time() + CB_OPEN_SECONDS
        b["reason"] = reason
        if not b.get("opened_at"):
            b["opened_at"] = time.time()
            transition = ("open", b["failures"], reason)
    if transition:
        _note_breaker_transition(provider, *transition)


def _note_breaker_transition(provider, kind, failures, reason):
    label = VENDOR_LABELS.get(provider, provider)
    if kind == "open":
        detail = (f"{failures} consecutive failures ({reason or 'provider errors'}) — "
                  f"calls fail fast for {CB_OPEN_SECONDS}s, then one probe")
        record_health_event(provider, "breaker_open", scope=reason, detail=detail)
        _page(f"breaker:{provider}", f"Cavnar AI: {label} calls are paused",
              [f"The {label} circuit breaker opened: {detail}.",
               "Every call to it is refused until a probe succeeds. Open the admin console → "
               "Operations → AI for the errors; Reset breaker forces a retry."])
    else:
        record_health_event(provider, "breaker_close", scope=reason,
                            detail=f"a call succeeded after the breaker opened ({reason or 'provider errors'})")


def reset_breaker(provider=None, actor=None):
    """Test hook and an operator escape hatch (the console's Reset breaker).
    With an actor the reset is recorded. Process-local: it clears this
    process's breaker, which is the one its request threads consult."""
    with _breaker_lock:
        if provider:
            _breakers.pop(provider, None)
        else:
            _breakers.clear()
    if actor:
        for p in ([provider] if provider else BREAKER_PROVIDERS):
            record_health_event(p, "breaker_reset", detail=f"reset by {actor}")


def user_facing_error(exc, fallback="Couldn't get an answer right now — try again in a moment."):
    """The message to show an owner when an AI call failed.

    AIBudgetExceeded carries a deliberately written, actionable sentence
    ("AI is paused — this account has reached its monthly budget. Contact
    will@cavnar.ai if this looks wrong."). The resiliency audit found it was
    caught specifically in exactly ONE place in the product — the invoice
    scanner — so everywhere else, including Ask, a budget-paused account was
    told "try again", forever, about a condition that retrying cannot clear.

    Returns (message, http_status). 429 for a budget stop, because it is a
    rate/quota condition the caller should not hammer; the fallback keeps
    whatever the call site already decided.
    """
    if isinstance(exc, AIBudgetExceeded):
        return str(exc), 429
    if isinstance(exc, AIProviderDown):
        return str(exc), 503
    if isinstance(exc, AIRefused):
        return ("Cavnar AI can't answer that one as asked. Try rephrasing it, "
                "or ask about a specific part of the business."), 422
    return fallback, 502


INSIGHT_RETRY_LATER = "Analysis unavailable — check back shortly."


def insight_error(exc, fallback=INSIGHT_RETRY_LATER):
    """(message, http_status) for an AI insight panel that failed.

    The panels (reviews, food cost, labor, marketing, web and phone) all
    answered every failure with "check back shortly" — including a budget
    stop, which checking back never clears (AI-11). A pause or an outage now
    says so in the words user_facing_error already has for it; anything else
    keeps the retry wording and the 500 those panels have always returned.
    Never the exception text itself (AI-31): that carries provider request
    ids and raw error bodies."""
    msg, status = user_facing_error(exc, fallback=fallback)
    return msg, (500 if status == 502 else status)


# ── Models and the client ─────────────────────────────────────────────────
#
# Every call site used to carry its own `os.getenv("X_MODEL", "claude-...")`
# literal (22 of them, 12 override names, two with no override at all) and
# nineteen of them built their own anthropic.Anthropic — six at import time,
# with the key frozen before load_dotenv could run. One registry and one
# factory; PROMPT_LIBRARY.md's table is generated from the same names.

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"
# The week's schedule is the hardest constraint problem the product asks a
# model to solve: presence per half hour, weekly hours across payroll weeks,
# a manager every minute, minors, rest. It runs on Opus 5.5 with its
# adaptive thinking (schedule audit 10/3/26 PR-6; owner, 10/3/26: "use opus
# 5.5 for the model call instead of sonnet"), at medium effort since 10/6/26
# (labor.SCHEDULE_EFFORT).
OPUS_55 = "claude-opus-5-5"
SONNET_55 = "claude-sonnet-5-5"

# purpose -> (env override, default). A default here is what the call site
# used before; an override name here is the one it read before, except
# REVIEW_ANALYSIS_MODEL and SALES_AUDIT_NOTES_MODEL, which are new because
# those two sites had none (inheriting CLAUDE_MODEL would have changed them
# wherever that variable is set).
MODELS = {
    "review_analysis":     ("REVIEW_ANALYSIS_MODEL",  HAIKU),
    "review_diagnosis":    ("CLAUDE_REPORTER_MODEL",  SONNET),
    "review_insight":      ("REVIEW_INSIGHT_MODEL",   SONNET),
    "drafter":             ("DRAFTER_MODEL",          SONNET),
    "inventory_insight":   ("INVENTORY_INSIGHT_MODEL", SONNET),
    "food_cost_diagnosis": ("CLAUDE_REPORTER_MODEL",  SONNET),
    "labor_insight":       ("LABOR_INSIGHT_MODEL",    SONNET),
    "schedule":            ("SCHEDULE_MODEL",         OPUS_55),
    "competitor_extract":  ("CLAUDE_MODEL",           HAIKU),
    "competitor_insight":  ("CLAUDE_REPORTER_MODEL",  SONNET),
    "marketing":           ("MARKETING_MODEL",        SONNET),
    "marketing_insight":   ("CLAUDE_MODEL",           HAIKU),
    "guest_marketing":     ("GUEST_MARKETING_MODEL",  SONNET),
    "recipes":             ("RECIPE_MODEL",           SONNET),
    "invoices":            ("INVOICE_MODEL",          OPUS),
    "reporter":            ("CLAUDE_REPORTER_MODEL",  SONNET),
    "email_personalise":   ("CLAUDE_MODEL",           HAIKU),
    "sales_audit_notes":   ("SALES_AUDIT_NOTES_MODEL", SONNET),
    "ask_cavnar":          ("ASK_CAVNAR_MODEL",       SONNET),
    # The rolling summary of an Ask chat's older turns (ask_conversations,
    # memory audit 9/29/26): short structured notes, validated per line.
    "ask_summary":         ("ASK_SUMMARY_MODEL",      HAIKU),
    "dsr_narrative":       ("DSR_NARRATIVE_MODEL",    SONNET),
    # A starter task sheet the owner accepts line by line (task_sheets.starter_lines).
    "task_sheets":         ("TASK_SHEET_MODEL",       SONNET),
    # Read by staff (employee audit B7, 10/1/26): the day's one rewrite of
    # the lineup notes a manager approves (staff_brief.draft), a translation
    # of manager-approved text (staff_knowledge.translate_for), and an
    # answer from the house rules that cites its lines (staff_knowledge.answer).
    "staff_brief":         ("STAFF_BRIEF_MODEL",      HAIKU),
    "staff_translation":   ("STAFF_TRANSLATION_MODEL", HAIKU),
    "staff_answer":        ("STAFF_ANSWER_MODEL",     SONNET),
}


# Models on which thinking cannot be turned off: an explicit
# {"type": "disabled"} is a 400 on every call (AI-14). Thinking is always on
# for these, so the parameter is omitted; extract_text already skips the
# thinking blocks they return. Prefix-matched so point releases are covered.
_THINKING_ALWAYS_ON_PREFIXES = ("claude-fable", "claude-mythos", "claude-opus-5-5")


# Models whose thinking is on by default and that refuse {"type": "disabled"}
# with a 400, but have a lowest setting of their own: Sonnet 5.5's is
# "between_tools" (no up-front thinking), valid up to effort high (schedule
# audit 10/3/26 PR-29 — moving any call site to Sonnet 5.5 would otherwise
# have failed every call, the AI-14 class).
_THINKING_LOWEST = (("claude-sonnet-5-5", {"type": "between_tools"}),)


def _effort_of(kwargs):
    return ((kwargs or {}).get("output_config") or {}).get("effort")


def accepts_disabled_thinking(model, kwargs=None):
    """Whether `model` accepts thinking={"type": "disabled"} on this call.

    Forcing it on a model that rejects it turned one env override into every
    AI feature failing with a 400 — and a 400 is not retryable, so the breaker
    never opened to say so. Claude Opus 5 accepts it only at effort high or
    below; Sonnet 5.5 never does."""
    m = (model or "").lower()
    if m.startswith(_THINKING_ALWAYS_ON_PREFIXES):
        return False
    if any(m.startswith(prefix) for prefix, _cfg in _THINKING_LOWEST):
        return False
    if m.startswith("claude-opus-5"):
        if _effort_of(kwargs) in ("xhigh", "max"):
            return False
    return True


def default_thinking(model, kwargs=None):
    """The thinking setting a call gets when its caller names none: off where
    the model allows it, the model's lowest setting where it refuses "off",
    and nothing at all (the model's own adaptive default) where thinking is
    always on or the effort asked for needs it."""
    if accepts_disabled_thinking(model, kwargs):
        return {"type": "disabled"}
    m = (model or "").lower()
    for prefix, cfg in _THINKING_LOWEST:
        if m.startswith(prefix):
            return None if _effort_of(kwargs) in ("xhigh", "max") else dict(cfg)
    return None


# The effort a call on a model whose thinking cannot be turned off gets when
# its caller names neither thinking nor an effort: the nearest thing to the
# "off" every other call site was written for. Claude Opus 5.5 thinks at
# medium effort by default and its thinking shares max_tokens, so a call
# site written for 500-1,600 tokens and moved to it by a *_MODEL override
# could spend the whole budget thinking and answer nothing (schedule audit
# 10/3/26 PR-29). A call that wants more says so (the schedule: high).
ALWAYS_THINKING_DEFAULT_EFFORT = "low"


def default_effort(model, kwargs=None):
    """The output_config.effort a call gets when its caller set none: low on
    a model whose thinking is always on (when the caller named no thinking
    either), else None — the model's own default."""
    kw = kwargs or {}
    if "thinking" in kw or _effort_of(kw):
        return None
    if (model or "").lower().startswith(_THINKING_ALWAYS_ON_PREFIXES):
        return ALWAYS_THINKING_DEFAULT_EFFORT
    return None


def model_for(purpose: str) -> str:
    """The model a call site uses: its env override if set, else its default."""
    env, default = MODELS[purpose]
    return os.getenv(env, default)


_clients = {}
_clients_lock = threading.Lock()


# The SDK's defaults are a 600 s read timeout and 2 retries of its own,
# inside create_with_retry's 2. With gunicorn's 4 request threads, four slow
# calls held the whole platform — login, the portal, webhooks and /health —
# for up to half an hour (AI-1). A request-path call waits at most this long
# for the answer; background work that writes long outputs (the schedule)
# asks for more explicitly.
DEFAULT_AI_TIMEOUT = 90.0
AI_CONNECT_TIMEOUT = 5.0


def get_client(timeout=None):
    """The shared anthropic.Anthropic for the current API key, built on first
    use rather than at import, so load_dotenv has run and a rotated key is
    picked up by the next call. One client per (key, timeout); the SDK's
    client is thread-safe and pools connections.

    Always bounded (DEFAULT_AI_TIMEOUT unless a caller names its own) and
    never retrying on its own: create_with_retry is the one retry loop, so
    a failing call is attempted retries+1 times, not (retries+1) x 3."""
    key = os.getenv("ANTHROPIC_API_KEY") or ""
    read = float(timeout if timeout is not None else DEFAULT_AI_TIMEOUT)
    cache_key = (key, read)
    with _clients_lock:
        c = _clients.get(cache_key)
        if c is None:
            c = anthropic.Anthropic(api_key=key,
                                    timeout=anthropic.Timeout(read, connect=AI_CONNECT_TIMEOUT),
                                    max_retries=0)
            _clients[cache_key] = c
        return c


# ── attribution: who or what made this call (#148) ─────────────────────────
#
# Every ledger row says what triggered it — the owner (a request with a
# client session), an admin (the console, or a view-as session), the
# scheduler, or the system (a webhook, a boot task) — which user, and which
# unit of work it belongs to (an Ask turn, a visibility run, a weekly plan),
# so "who or what spent this" has an answer and one answer's rounds can be
# summed. Call sites that know better say so with ai_context(); everything
# else is attributed from the request or, off a request, the call stack.
TRIGGERS = ("owner", "scheduler", "admin", "system")

_CTX = contextvars.ContextVar("cavnar_ai_ctx", default=None)
# The last provider call made in this context — what validation rows and
# stored reads link to (#117). A request thread is reused, so the link only
# holds for a short while: a verdict or a stored read follows its call within
# seconds, and an older call on the same thread is some other request's.
_LAST_CALL = contextvars.ContextVar("cavnar_ai_last_call", default=None)
_LAST_CALL_LINK_SECONDS = 120


@contextlib.contextmanager
def ai_context(trigger=None, actor_user_id=None, correlation_id=None, restaurant_id=None, action=None):
    """Attribute every AI and Places call made inside the block. Nests: an
    inner block overrides only what it names. `restaurant_id`/`action` are
    the default attribution for Places requests (places_request) made by
    helpers that take no restaurant of their own."""
    cur = dict(_CTX.get() or {})
    for k, v in (("trigger", trigger), ("actor_user_id", actor_user_id), ("correlation_id", correlation_id),
                 ("restaurant_id", restaurant_id), ("action", action)):
        if v is not None:
            cur[k] = v
    token = _CTX.set(cur)
    try:
        yield cur
    finally:
        _CTX.reset(token)


def current_ai_context() -> dict:
    """The attribution in force here (a copy): what an enclosing ai_context
    named. Empty outside one."""
    return dict(_CTX.get() or {})


def attribution_for_thread() -> dict:
    """This work's attribution as ai_context keyword arguments — taken on the
    thread that hands work to a background thread (ops.run_admin_task, an
    owner's generation job) and re-entered there, where the request that
    said who asked is gone and the calls would read as 'system' (#148)."""
    try:
        trigger, actor, corr = _attribution()
    except Exception:
        trigger, actor, corr = None, None, None
    return {"trigger": trigger, "actor_user_id": actor, "correlation_id": corr}


def attributed(fn):
    """`fn`, run under the attribution of the moment it was handed over — for
    a thread a request starts (an owner's schedule generation, an Ask stream).
    A new thread starts with no ai_context and no request, so its calls read
    as 'system' and nobody's (#148, G's integration request)."""
    attr = {k: v for k, v in attribution_for_thread().items() if v is not None}

    def _run(*args, **kwargs):
        with ai_context(**attr):
            return fn(*args, **kwargs)
    _run.__name__ = getattr(fn, "__name__", "attributed")
    return _run


def new_correlation_id(prefix="run"):
    """A fresh id for one unit of work ("ask:…", "aivis:…")."""
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def context_runner(fn):
    """`fn` wrapped to run in the context current HERE (the submitting
    thread's) — for work handed to a thread pool, which does not inherit
    contextvars. The snapshot is taken now; each call runs in its own copy
    of it, since one Context cannot be entered by two threads at once."""
    snapshot = contextvars.copy_context()

    def run(*a, **k):
        return snapshot.copy().run(fn, *a, **k)
    return run


def _request_attribution():
    """(trigger, actor_user_id, restaurant_id) for the Flask request this call
    is made from, or (None, None, None) off a request. One indexed read of the
    session (cached on flask.g for the request); never writes, never raises.
    A view-as session is the admin who opened it (owner decision 3)."""
    try:
        from flask import g, has_request_context, request
    except Exception:
        return None, None, None
    try:
        if not has_request_context():
            return None, None, None
        # The login decorators put a view-as session's context on flask.g
        # (auth._view_as_context): the admin behind it is the actor, and no
        # read is needed. The session row below is for a call made before
        # (or without) the decorator.
        va = getattr(g, "view_as", None)
        if isinstance(va, dict) and va.get("acting_admin_id"):
            return "admin", va["acting_admin_id"], va.get("restaurant_id")
        cached = getattr(g, "_cavnar_ai_actor", None)
        if cached is not None:
            return cached
        path = request.path or ""
        trigger = "admin" if path.startswith("/admin") else None
        actor = rid = None
        token = request.cookies.get("session_token") or ""
        if not token:
            header = request.headers.get("Authorization", "") or ""
            token = header[7:].strip() if header.startswith("Bearer ") else ""
        if token:
            try:
                from auth import hash_session_token
                conn = _conn()
                try:
                    th = hash_session_token(token)
                    # The admin behind a view-as is on the session row
                    # (sessions.acting_admin_id, fix round A); a database
                    # from before that column reads view_as_sessions.
                    try:
                        row = conn.execute(
                            "SELECT s.user_id, s.device_type, s.active_restaurant_id, s.acting_admin_id, "
                            "u.restaurant_id AS home, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
                            "WHERE s.token=?", (th,)).fetchone()
                    except sqlite3.OperationalError:
                        row = conn.execute(
                            "SELECT s.user_id, s.device_type, s.active_restaurant_id, NULL AS acting_admin_id, "
                            "u.restaurant_id AS home, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id "
                            "WHERE s.token=?", (th,)).fetchone()
                    if row:
                        actor = row["user_id"]
                        rid = row["active_restaurant_id"] or row["home"]
                        if (row["device_type"] or "") == "admin-view-as":
                            trigger = "admin"
                            if row["acting_admin_id"]:
                                actor = row["acting_admin_id"]
                            else:
                                try:
                                    va = conn.execute("SELECT opened_by FROM view_as_sessions WHERE token_hash=?",
                                                      (th,)).fetchone()
                                    if va and va["opened_by"]:
                                        actor = va["opened_by"]
                                except sqlite3.Error:
                                    pass
                        elif row["is_admin"]:
                            trigger = "admin"
                        trigger = trigger or "owner"
                finally:
                    conn.close()
            except Exception as e:
                log.debug("request attribution unavailable: %s", e)
        # A request with no session is a webhook or a public endpoint; one
        # whose session could not be read is still a signed-in person's.
        out = (trigger or ("owner" if token else "system"), actor, rid)
        g._cavnar_ai_actor = out
        return out
    except Exception:
        return None, None, None


# Frames that mark work the scheduler (or an admin action run off-request)
# is doing, read when a call has neither an explicit context nor a request.
_SCHEDULER_FILES = ("scheduler.py", "worker.py", "strategy_jobs.py", "billing_jobs.py")
_ADMIN_FILES = ("admin_ops.py", "admin_routes.py")


def _stack_trigger(max_depth=80):
    """'scheduler' / 'admin' from the call stack, or None. Off a request, the
    stack is the only witness: a job runs through ops.run_job or scheduler.py,
    an admin "run now" through admin_ops."""
    try:
        f = sys._getframe(2)
    except ValueError:
        return None
    depth = 0
    while f is not None and depth < max_depth:
        fname = os.path.basename(f.f_code.co_filename or "")
        if fname in _ADMIN_FILES:
            return "admin"
        if fname in _SCHEDULER_FILES or (fname == "ops.py" and f.f_code.co_name == "run_job"):
            return "scheduler"
        f = f.f_back
        depth += 1
    return None


def _attribution():
    """(trigger, actor_user_id, correlation_id) for the call being logged:
    ai_context first, then the request, then the stack; 'system' when
    nothing says otherwise."""
    ctx = _CTX.get() or {}
    trigger, actor, corr = ctx.get("trigger"), ctx.get("actor_user_id"), ctx.get("correlation_id")
    if not trigger or actor is None:
        rt, ra, _rid = _request_attribution()
        trigger = trigger or rt
        actor = actor if actor is not None else ra
    if not trigger:
        trigger = _stack_trigger()
    return (trigger if trigger in TRIGGERS else "system"), actor, corr


def _context_restaurant():
    """The restaurant ai_context (or the request's session) names, for a
    Places request made by a helper that takes no restaurant of its own."""
    ctx = _CTX.get() or {}
    if ctx.get("restaurant_id") is not None:
        return ctx.get("restaurant_id")
    return _request_attribution()[2]


def _note_last_call(call_id, restaurant_id, action):
    _LAST_CALL.set({"call_id": call_id, "restaurant_id": restaurant_id, "action": action, "at": time.time()})


# Validation surfaces and the ledger actions their model calls log under, so
# a validation row links to the call it checked and not to an unrelated one
# made earlier on the same thread.
_SURFACE_ACTIONS = {
    "food_insight": {"inventory_insight"}, "intel": {"competitor_insight"},
    "digest": {"weekly_digest"}, "dsr": {"dsr_narrative"}, "ask": {"ask_cavnar", "weekly_plan"},
    "weekly_plan": {"weekly_plan", "ask_cavnar"}, "reply_public": {"draft_response"},
    "review_diagnosis": {"review_diagnosis"}, "food_diagnosis": {"food_cost_diagnosis"},
    "labor_insight": {"labor_insight"}, "schedule_note": {"labor_schedule"},
    "review_insight": {"review_insight"}, "marketing_insight": {"marketing_insight"},
    "email_personalise": {"email_personalization"}, "guest_sms": {"guest_campaign_draft", "guest_newsletter_draft"},
    "social_post": {"marketing_content", "guest_newsletter_draft"}, "calendar_idea": {"content_calendar"},
}


def last_call_id(restaurant_id=None, action=None, surface=None):
    """The call_id of the last provider call in this context, when it is the
    one being talked about: same restaurant, recent, and (when an action or
    surface is given) of a matching kind. None otherwise — a wrong link is
    worse than none."""
    last = _LAST_CALL.get()
    if not last or time.time() - last.get("at", 0) > _LAST_CALL_LINK_SECONDS:
        return None
    if restaurant_id is not None and last.get("restaurant_id") not in (None, restaurant_id):
        return None
    if action or surface:
        allowed = set(_SURFACE_ACTIONS.get(surface or "", ())) | {a for a in (action, surface) if a}
        if last.get("action") not in allowed:
            return None
    return last.get("call_id")


def _new_call_id():
    return uuid.uuid4().hex[:20]


def _is_retryable(exc):
    if isinstance(exc, _RETRYABLE):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(exc, anthropic.APIStatusError) and isinstance(status, int):
        return status in _RETRYABLE_STATUS or status >= 500
    return type(exc).__name__ in ("OverloadedError", "ServiceUnavailableError", "DeadlineExceededError")


def classify_error(exc):
    """A short machine reason for a failed provider call: timeout, connection,
    rate_limit, overloaded, server, auth, permission, credit, not_found,
    too_large, bad_request, unknown."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, (anthropic.APITimeoutError, CallDeadlineExceeded)):
        return "timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection"
    if status == 529 or name == "OverloadedError" or "overloaded" in text:
        return "overloaded"
    if isinstance(exc, anthropic.RateLimitError) or status == 429:
        return "rate_limit"
    if isinstance(exc, anthropic.AuthenticationError) or status == 401:
        return "auth"
    if isinstance(exc, anthropic.PermissionDeniedError) or status == 403:
        return "permission"
    if "credit balance" in text or ("billing" in text and status == 400):
        return "credit"
    if isinstance(exc, anthropic.NotFoundError) or status == 404:
        return "not_found"
    if status == 413:
        return "too_large"
    if isinstance(status, int) and status >= 500:
        return "server"
    if isinstance(exc, anthropic.BadRequestError) or status == 400:
        return "bad_request"
    return "unknown"


# Failures that say the provider rejects US, not the request: every call will
# fail the same way until someone changes a key, a balance or a model id.
_CREDENTIAL_REASONS = ("auth", "permission", "credit")
_CONFIG_REASONS = _CREDENTIAL_REASONS + ("not_found",)


def _note_provider_error(provider, reason, exc):
    """A credential, credit or model-id failure — or an overload that used up
    every retry — recorded, and paged once an hour (#104). It used to be one
    ai_usage error row that nobody was told about."""
    label = VENDOR_LABELS.get(provider, provider)
    event = {"auth": "auth_error", "permission": "auth_error", "credit": "credit_error",
             "not_found": "not_found", "overloaded": "overloaded"}.get(reason, "provider_error")
    try:
        from ai_guard import safe_error
        detail = safe_error(exc)[:240]
    except Exception:
        detail = type(exc).__name__
    record_health_event(provider, event, scope=reason, detail=detail)
    if reason in _CONFIG_REASONS:
        what = {"auth": "rejected the API key", "permission": "refused the request (403)",
                "credit": "reports the credit balance is exhausted",
                "not_found": "does not know the model id"}.get(reason, reason)
        _page(f"{event}:{provider}", f"Cavnar AI: {label} {what}",
              [f"{label} {what}: {detail}",
               "Every call to it fails the same way until this is fixed — check the key, the balance "
               "or the *_MODEL variables on Railway."])


class CallDeadlineExceeded(TimeoutError):
    """A call stopped because the job that made it ran out of its wall-clock
    time (create_with_retry's `deadline`; schedule audit 10/3/26 P-22). A
    streamed answer's timeout is the longest silence between events, so a
    call that kept streaming could run past any deadline — the schedule's
    up to ~18 minutes a slice. `partial` is the Message as far as it had
    streamed (None when nothing had), for a caller that can keep what was
    already written; `call_id` is the ledger row it was filed under."""

    def __init__(self, message="the call ran past its deadline", partial=None):
        super().__init__(message)
        self.partial = partial
        self.call_id = None


def _attempt_timeout(client, deadline):
    """The timeout one attempt gets under a deadline: the client's own read
    timeout, never past the time left (P-22: "each call's timeout =
    min(360, time remaining)"). Connecting keeps its own short limit."""
    left = float(deadline) - time.time()
    t = getattr(client, "timeout", None)
    read = getattr(t, "read", t)
    try:
        cap = float(read)
    except (TypeError, ValueError):
        cap = DEFAULT_AI_TIMEOUT
    secs = max(1.0, min(cap, left))
    return anthropic.Timeout(secs, connect=min(AI_CONNECT_TIMEOUT, secs))


def _send(client, kwargs, stream=False, deadline=None):
    """One attempt: messages.create, or messages.stream collected into the
    finished Message when the caller asked to stream. A test double with no
    stream() is called the ordinary way.

    With a `deadline` a stream is read event by event and cut when the time
    is up: its timeout only bounds the silence between events, so a call
    that kept writing used to run on regardless (P-22). The cut raises
    CallDeadlineExceeded carrying what had streamed."""
    if stream:
        streamer = getattr(getattr(client, "messages", None), "stream", None)
        if callable(streamer):
            with streamer(**kwargs) as s:
                if deadline is None:
                    return s.get_final_message()
                for _event in s:
                    if time.time() >= float(deadline):
                        try:
                            partial = s.current_message_snapshot
                        except Exception:
                            partial = None
                        s.close()
                        raise CallDeadlineExceeded(partial=partial)
                return s.get_final_message()
    return client.messages.create(**kwargs)


def create_with_retry(client, retries=2, backoff=1.5, restaurant_id=None, action=None, readiness=None, **kwargs):
    """client.messages.create(**kwargs) with exponential backoff on
    transient failures. Raises the last exception if all attempts fail.

    Extended thinking is off by default: newer Sonnet models will prepend a
    ThinkingBlock to `message.content` for anything past a trivial prompt,
    which breaks every `message.content[0].text` call site in this codebase
    (there are 14 of them) with an AttributeError, and burns max_tokens on
    reasoning the app never reads — every use here is short, deterministic,
    format-constrained generation that doesn't need chain-of-thought.
    Callers that ever want it can still pass thinking=... explicitly.

    Every call funnels through here, which makes it the one place to log
    spend — pass restaurant_id/action (both optional) and usage is recorded
    to the ai_usage table on success. Neither is forwarded to the Anthropic
    API; they're popped off before reaching client.messages.create().

    `readiness` is data_health.readiness()'s answer for this call (or
    data_health.NOT_APPLICABLE for a call that rests on no data source). A
    "refuse" raises DataNotReady before any network call — it costs no
    tokens — and so does "wait" (unattended output whose data is retrying).
    Every call site passes it explicitly — tests/test_readiness_adoption.py
    fails on one that does not; None is still tolerated at runtime (a test
    double, an old caller) and changes nothing.

    Every outcome is a ledger row (#48, #52): a call refused before it left —
    readiness, budget, breaker — is a zero-cost 'blocked' row with its
    reason; a returned message is 'ok', 'refused' or 'truncated' by its
    stop_reason; a failure is 'error' with its class. Each row carries the
    attempts, the total latency including backoff, the provider request id,
    who or what triggered it and a call id shared with its ai_calls trace.
    The returned message carries that id as `_cavnar_call_id`
    (mark_outcome() files an 'unparseable' against it)."""
    model = kwargs.get("model", "unknown")
    trigger, actor, corr = _attribution()
    attribution = {"trigger": trigger, "actor_user_id": actor, "correlation_id": corr}
    if isinstance(readiness, dict) and readiness.get("decision") in ("refuse", "wait"):
        log_blocked(restaurant_id, action, model, "data_not_ready",
                    detail=str(readiness.get("reason") or readiness.get("decision"))[:200], **attribution)
        raise DataNotReady(readiness)
    _effort = default_effort(kwargs.get("model"), kwargs)
    if _effort:
        kwargs["output_config"] = dict(kwargs.get("output_config") or {}, effort=_effort)
    if "thinking" not in kwargs:
        _thinking = default_thinking(kwargs.get("model"), kwargs)
        if _thinking is not None:
            kwargs["thinking"] = _thinking
    # A long answer under thinking (the schedule: tens of thousands of output
    # tokens at high effort) is streamed: a non-streaming request that runs
    # for minutes can be cut by any idle connection on the way, and the SDK
    # refuses one past ~21k max_tokens on a default client. The caller asks
    # with stream=True and still gets one finished Message back.
    stream = bool(kwargs.pop("stream", False))
    # A job with a wall-clock limit of its own (the schedule generation:
    # schedule audit 10/3/26 P-22) passes `deadline` (a time.time() value):
    # no retry starts past it, each attempt's timeout is the time left at
    # most, a timed-out attempt is not sent again as it was (its caller
    # re-plans it smaller), and a stream still writing at the deadline is
    # cut (CallDeadlineExceeded, carrying what it had written) — so one slice
    # can never outlast the job.
    deadline = kwargs.pop("deadline", None)
    # anthropic>=0.105 (what Railway installs) rejects `temperature` outright
    # — TypeError before the request is even made — and current Sonnet
    # models refuse it server-side anyway. Strip it here so no caller can
    # take production down with a parameter that never mattered.
    kwargs.pop("temperature", None)
    # Past the job's deadline no call is sent at all (P-22): it could only be
    # cut, and a streamed call is billed for the input it sent. A zero-cost
    # 'blocked' row, like the budget and the breaker.
    if deadline is not None and time.time() >= float(deadline):
        log_blocked(restaurant_id, action, model, "deadline", detail="the job's time ran out before the call",
                    **attribution)
        raise CallDeadlineExceeded("no time was left for this call")
    # One positional argument: callers and tests replace this function with
    # their own; it reads the trigger itself (an admin's call answers only
    # to the global pool, #148).
    over = ai_budget_exceeded(restaurant_id)
    if over:
        _record_budget_stop(over, restaurant_id)
        log_blocked(restaurant_id, action, model, "budget", detail=over, **attribution)
        raise AIBudgetExceeded(
            f"AI is paused — this account has reached its {over}. "
            "Contact will@cavnar.ai if this looks wrong."
        )
    try:
        _breaker_check("anthropic")
    except AIProviderDown:
        log_blocked(restaurant_id, action, model, "breaker", detail="the provider breaker is open", **attribution)
        raise
    call_id = _new_call_id()
    attempt = 0
    # Wall time from the first attempt to the answer, backoff included, so
    # "is Ask slow?" has an answer and a success after two 429s does not
    # look clean (#52).
    started = time.time()
    while True:
        try:
            if deadline is not None:
                # Each attempt waits at most the time left (P-22).
                kwargs["timeout"] = _attempt_timeout(client, deadline)
            message = _send(client, kwargs, stream, deadline=deadline)
        except CallDeadlineExceeded as e:
            # The job's own clock, not the provider: never a breaker failure,
            # and what streamed before the cut is billed, so it is filed with
            # its tokens as a truncated answer (#52).
            e.call_id = call_id
            latency = int((time.time() - started) * 1000)
            _log_cut_safe(e.partial, model, restaurant_id, action, latency, attempt + 1, call_id, attribution)
            _record_trace_safe(call_id, kwargs, e.partial, restaurant_id, action, "truncated", attribution,
                               attempts=attempt + 1, latency_ms=latency)
            _breaker_release_probe("anthropic")
            raise
        except Exception as e:
            reason = classify_error(e)
            # Under a deadline a timed-out call is not sent again as it was:
            # the same call would time out again in less time, and the caller
            # re-plans it smaller (the schedule splits it; P-22).
            if _is_retryable(e) and not (deadline is not None and reason == "timeout"):
                attempt += 1
                if attempt <= retries and (deadline is None or time.time() + backoff ** attempt < float(deadline)):
                    time.sleep(backoff ** attempt)
                    continue
                # Retry budget exhausted — this is the "AI is down" signal the
                # operator digest exists for, so record it before re-raising.
                _log_failure_safe(e, model, restaurant_id, action, attempts=attempt, reason=reason,
                                  latency_ms=int((time.time() - started) * 1000), call_id=call_id,
                                  attribution=attribution)
                _record_trace_safe(call_id, kwargs, None, restaurant_id, action, "error", attribution,
                                   attempts=attempt, latency_ms=int((time.time() - started) * 1000))
                try:
                    import ops
                    ops.capture(e, job="ai_call", context=str(model))
                except Exception:
                    pass
                if reason == "overloaded":
                    _note_provider_error("anthropic", reason, e)
                # Only an EXHAUSTED retry budget counts toward the breaker:
                # one transient 429 that the retry absorbed is the system
                # working, not a provider that is down.
                _breaker_record("anthropic", False, reason=reason)
                raise
            # Not retryable (bad request, auth, a malformed response) — still
            # a failed AI call the admin console should see next to the
            # successes, in the same table.
            _log_failure_safe(e, model, restaurant_id, action, attempts=attempt + 1, reason=reason,
                              latency_ms=int((time.time() - started) * 1000), call_id=call_id,
                              attribution=attribution)
            _record_trace_safe(call_id, kwargs, None, restaurant_id, action, "error", attribution,
                               attempts=attempt + 1, latency_ms=int((time.time() - started) * 1000))
            if reason in _CONFIG_REASONS:
                _note_provider_error("anthropic", reason, e)
            if reason in ("auth", "credit"):
                # A revoked key or an empty balance fails every call alike:
                # stop sending them, and let the probe find the fix.
                trip_breaker("anthropic", reason)
            else:
                _breaker_release_probe("anthropic")
            raise
        latency_ms = int((time.time() - started) * 1000)
        usage_id = _log_usage_safe(message, model, restaurant_id, action, latency_ms=latency_ms,
                                   attempts=attempt + 1, call_id=call_id, attribution=attribution)
        _breaker_record("anthropic", True)
        if getattr(message, "usage", None) is not None:
            _record_trace_safe(call_id, kwargs, message, restaurant_id, action, outcome_of(message), attribution,
                               attempts=attempt + 1, latency_ms=latency_ms, usage_id=usage_id)
            _note_last_call(call_id, restaurant_id, action or "unspecified")
            try:
                setattr(message, "_cavnar_call_id", call_id)
            except Exception:
                pass
        return message


def with_data_state(prompt, readiness) -> str:
    """`prompt` with readiness's DATA STATE block appended (data_health.
    readiness), so the model knows how current each source is before it
    writes (DH1-2, DH5-2). Unchanged when there is no block."""
    block = str((readiness or {}).get("prompt_block") or "").strip()
    return f"{prompt}\n\n{block}" if block else prompt


def is_held(readiness) -> bool:
    """True when readiness says the call must not run (refuse, or wait for
    a retry that is due) — the check a caller makes before building a
    prompt it would only throw away."""
    return isinstance(readiness, dict) and readiness.get("decision") in ("refuse", "wait")


# ── AI cost/usage tracking ──────────────────────────────────────────────────
# Dozens of call sites across analyser/drafter/competitor/labor/marketing/
# inventory/reporter call Claude with zero visibility into what any of it
# costs — no way to see per-restaurant spend as client count grows, or
# whether one client's usage pattern (e.g. mashing "Regenerate") is eating
# margin. Every create_with_retry() call now logs here — every outcome, not
# only the successes (#48, #52): ok, error, refused, truncated, unparseable
# and blocked (refused before it reached a provider), with the stop reason,
# attempts, total latency, provider request id, trigger, actor, correlation
# id, price-table version and the call id its ai_calls trace carries.
#
# `trigger` is a keyword in SQLite's grammar but a legal column name; the SQL
# here quotes it anyway.
_USAGE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER,
    action TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_write_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER,
    cost_usd REAL,
    created_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'ok',
    error TEXT,
    vendor TEXT,
    outcome TEXT,
    stop_reason TEXT,
    attempts INTEGER,
    request_id TEXT,
    "trigger" TEXT,
    actor_user_id INTEGER,
    correlation_id TEXT,
    price_version TEXT,
    call_id TEXT,
    reason TEXT
)
"""

# _spend_since runs a SUM over this table on every AI call (behind a 60s
# cache) and the global variant joins restaurants on top. It had no index at
# all, so the budget check was a full scan of a ledger that nothing pruned —
# it got slower every day the product was used, on the hot path, on SQLite.
_USAGE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_usage_restaurant_created ON ai_usage(restaurant_id, created_at)",
)
# Indexes on columns added after the table shipped: created after the
# columns, or they fail on an old database and the whole ensure retries
# forever.
_USAGE_LATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_ai_usage_call ON ai_usage(call_id)",
)

# Columns added to ai_usage after it shipped, in the order they arrived.
_USAGE_COLUMNS = (
    ("status", "TEXT DEFAULT 'ok'"), ("error", "TEXT"),
    ("cache_write_tokens", "INTEGER DEFAULT 0"), ("cache_read_tokens", "INTEGER DEFAULT 0"),
    ("latency_ms", "INTEGER"),
    # Fix round G (9/29/26): the ledger records every outcome and who caused it.
    ("vendor", "TEXT"), ("outcome", "TEXT"), ("stop_reason", "TEXT"), ("attempts", "INTEGER"),
    ("request_id", "TEXT"), ("trigger", "TEXT"), ("actor_user_id", "INTEGER"),
    ("correlation_id", "TEXT"), ("price_version", "TEXT"), ("call_id", "TEXT"), ("reason", "TEXT"),
)

# Outcomes a ledger row can carry. status stays the coarse legacy reading
# every older query uses: 'ok' only for a usable answer, 'blocked' for a call
# that never left, 'error' for everything that came back unusable.
OUTCOMES = ("ok", "error", "refused", "truncated", "unparseable", "blocked")


def _status_for(outcome):
    if outcome == "ok":
        return "ok"
    if outcome == "blocked":
        return "blocked"
    return "error"


def outcome_of(message):
    """'ok', 'refused' or 'truncated' from a returned message's stop_reason.
    Running into the model's context window cuts an answer short exactly as
    max_tokens does (schedule audit 10/3/26 PR-30) — it read as 'ok'."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        return "refused"
    if stop in ("max_tokens", "model_context_window_exceeded"):
        return "truncated"
    return "ok"


def vendor_for(model):
    """The vendor a ledger model string belongs to."""
    m = (model or "").lower()
    if m.startswith("google-places") or m.startswith("google_places"):
        return "google_places"
    if m.startswith("perplexity") or m.startswith("sonar"):
        return "perplexity"
    return "anthropic"


# The usage table's schema setup ran on the HOT PATH: _spend_since executed
# CREATE TABLE before every AI call (the budget check), and log_ai_usage
# executed CREATE TABLE + two CREATE INDEX + a PRAGMA table_info + up to four
# ALTER checks after every one. init_db already owns this table; the repeated
# DDL was belt-and-braces for databases that predate it.
#
# Kept, but run ONCE per database per process. Keyed by db_path rather than a
# bare flag because the test suite points every test at its own fresh file,
# and a global flag would let the second test inherit the first's "already
# done" and run against a table that does not exist yet.
_usage_schema_ready = set()


def _ensure_usage_schema(conn, db_path, force=False):
    """Create/migrate the usage table, at most once per database per process.

    `force` re-runs it after a write failed on a missing column — the schema
    can change underneath a live process (a deploy migrating the volume while
    the previous instance is still serving, a restored backup, and the test
    that drops and recreates this table to prove the in-place migration still
    works). The once-per-process flag is a hot-path optimisation, not a claim
    that the schema is immutable. init_ai_ops runs it at boot too.
    """
    if db_path in _usage_schema_ready and not force:
        return
    try:
        conn.execute(_USAGE_TABLE_SQL)
        for _ix in _USAGE_INDEX_SQL:
            conn.execute(_ix)
        _ensure_usage_columns(conn)
        for _ix in _USAGE_LATE_INDEX_SQL:
            conn.execute(_ix)
        conn.commit()
        _usage_schema_ready.add(db_path)
    except Exception:
        # Leave it unmarked so the next call retries rather than reading a
        # table that was never created.
        pass


def _ensure_usage_columns(conn):
    """status/error arrived after the table existed on Railway, then the two
    cache columns, latency, and fix round G's outcome and attribution
    columns — add them in place so old rows keep their (implicit) 'ok' and
    their implicit zero cache usage. One ALTER failing (a concurrent
    process added it first) does not stop the rest."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_usage)").fetchall()}
    except Exception:
        return
    for name, typ in _USAGE_COLUMNS:
        if name in cols:
            continue
        try:
            conn.execute(f'ALTER TABLE ai_usage ADD COLUMN "{name}" {typ}')
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                log.warning("ai_usage column %s not added: %s", name, e)


# ── prices ──────────────────────────────────────────────────────────────────
#
# The version of the price table below, stamped on every ledger row, so a
# cost can be traced to the rates it was written at (#148). Change it when a
# rate changes. Rows written before versions existed carry 'legacy', or
# 'repriced-2026-09-22' where init_ai_ops corrected them once (Sonnet 5 at
# the $3/$15 Sonnet-4 rate until 9/22; Perplexity tokens at the Sonnet-4 or
# unknown-model rate).
PRICE_VERSION = "2026-10-04"   # cache reads priced per model (PROMPT-9)
REPRICED_VERSION = "repriced-2026-09-22"

# $ per million tokens (input, output), Anthropic list prices — update here if
# they change. Sonnet 5 was recorded at $3/$15 (the Sonnet 4.x price) against
# a list price of $2/$10, so every budget tripped 50% early (AI-12).
_MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    # invoices.py reads prices off photos with the most capable model.
    "claude-opus-5": (5.00, 25.00),
    # The week's schedule (schedule audit 10/3/26), and the budget tier its
    # evaluation runs against (scripts/schedule_model_eval.py).
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    # Perplexity sonar, per million tokens. Audit #7 found this vendor was
    # entirely outside the ledger and the budget — the $10/day and
    # $1,500/month ceilings bound Claude only, while AI visibility could fire
    # nine sonar queries a minute per restaurant, unmetered and unlogged.
    # Kept under its old name; _price_for maps every Perplexity id here
    # (#68: the ledger wrote 'perplexity-search', which matched no key and
    # was priced at the unknown-model $10/$50).
    "perplexity-sonar": (1.00, 1.00),
}

# Perplexity token prices by model (AI_VISIBILITY_MODEL picks one). An id not
# listed prices like the most expensive listed one, so the budget over-counts
# an unknown model rather than under-counting it.
_PERPLEXITY_PRICING = {
    "sonar": (1.00, 1.00),
    "sonar-pro": (3.00, 15.00),
    "sonar-reasoning": (1.00, 5.00),
    "sonar-reasoning-pro": (2.00, 8.00),
    "sonar-deep-research": (2.00, 8.00),
}
_PERPLEXITY_UNKNOWN = (3.00, 15.00)

# Vendors billed per REQUEST rather than per token. Same ledger, one row per
# request, so the console and the budgets see every paid dependency.
#
# Rates are list prices at the time of writing and are the thing most likely
# to drift here — they are env-overridable so a price change is a Railway
# variable, not a deploy.
_PER_CALL_PRICING = {
    # Perplexity charges a per-search fee on top of tokens.
    "perplexity-search":     float(os.getenv("PRICE_PERPLEXITY_SEARCH", "0.005")),
    # Google Places Details / Nearby / Text Search, roughly $17 / $32 / $32
    # per 1,000.
    "google-places-details": float(os.getenv("PRICE_PLACES_DETAILS", "0.017")),
    "google-places-nearby":  float(os.getenv("PRICE_PLACES_NEARBY", "0.032")),
    "google-places-textsearch": float(os.getenv("PRICE_PLACES_TEXTSEARCH", "0.032")),
}
# A vendor family named instead of a SKU (log_api_call's `vendor`).
_VENDOR_DEFAULT_SKU = {"perplexity": "perplexity-search", "google_places": "google-places-details"}


def log_api_call(restaurant_id, action, vendor, calls=1, input_tokens=0, output_tokens=0,
                 db_path=None, status="ok", error=None, model=None, outcome=None, attempts=None,
                 latency_ms=None, reason=None, request_id=None, correlation_id=None, call_id=None):
    """Record a non-Claude paid dependency in the same ledger.

    `vendor` is the billed SKU ('perplexity-search', 'google-places-details')
    or a vendor family ('perplexity', 'google_places'); `model` is what was
    actually asked (AI_VISIBILITY_MODEL for Perplexity, default the SKU). The
    row's `vendor` column is the family, its tokens are priced by the model
    and its per-request fee by the SKU. Perplexity counts toward the AI
    ceilings; Google Places toward its own (places_budget_status).
    """
    sku = _VENDOR_DEFAULT_SKU.get(vendor, vendor)
    family = vendor_for(sku)
    model = model or sku
    outcome = outcome or ("ok" if status == "ok" else ("blocked" if status == "blocked" else "error"))
    billed = outcome in ("ok", "refused", "truncated", "unparseable")
    per_call = _PER_CALL_PRICING.get(sku, 0.0) * max(0, int(calls or 0))
    token_cost = _estimate_cost(model, input_tokens, output_tokens) if (input_tokens or output_tokens) else 0.0
    cost = (per_call + token_cost) if billed else 0.0
    try:
        log_ai_usage(restaurant_id, action, model, int(input_tokens or 0), int(output_tokens or 0),
                     db_path=db_path, status=_status_for(outcome), error=error, latency_ms=latency_ms,
                     vendor=family, outcome=outcome, attempts=attempts, request_id=request_id,
                     correlation_id=correlation_id, call_id=call_id, reason=reason, cost_usd=cost)
    except Exception as e:
        log.warning("log_api_call(%s/%s) failed: %s", vendor, action, e)
    return cost


# Prompt caching is billed at its own rates, as a multiple of the model's
# input rate: writing a 5-minute cache entry costs 1.25x, reading one 0.1x on
# most models. Neither is included in `input_tokens`, which counts only the
# uncached remainder.
#
# This matters beyond reporting. Ask Cavnar now caches ~9,600 tokens of tools
# and static prompt, so a cache WRITE is a real charge the ledger recorded as
# nothing — and ai_budget_exceeded sums this ledger, so the $10/day and
# $1,500/month ceilings would have been enforced against an understated
# figure on every cached call. Counting reads matters the other way: they are
# the saving, and a cache that silently stops hitting is an expensive
# regression nothing would otherwise surface.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10
# A cache read is not 0.1x on every model (schedule re-audit 10/4/26
# PROMPT-9): Claude Opus 5.5 lists cache reads at $0.20/MTok against $4
# input — 0.05x — and Claude Fable 5.1 / Mythos 5.1 at $0.25 against $10 —
# 0.025x. Every cached schedule read was booked at twice its price, and that
# ledger is what ai_budget_exceeded sums. Longest prefix first; anything
# else reads at _CACHE_READ_MULTIPLIER. Verified against Anthropic's model
# reference of 9/25/26: Opus 5.5 $0.20, Fable 5.1 $0.25, Sonnet 5.5 $0.20 on
# $2 (0.1x), Fable 5 $1 on $10 (0.1x). The 1.25x write is the 5-minute TTL
# (every cache_control here is ephemeral 5m); a 1-hour TTL writes at 2x.
_CACHE_READ_BY_FAMILY = (
    ("claude-opus-5-5", 0.05),
    ("claude-fable-5-1", 0.025),
    ("claude-mythos-5-1", 0.025),
)


def _cache_read_multiplier(model) -> float:
    m = (model or "").lower()
    for prefix, mult in _CACHE_READ_BY_FAMILY:
        if m.startswith(prefix):
            return mult
    return _CACHE_READ_MULTIPLIER


# A model id the table does not name exactly — an alias (claude-haiku-4-5), a
# dated snapshot, an env override — is priced by its family, longest prefix
# first. It used to fall straight to Sonnet-4 pricing, so a Haiku alias was
# over-counted 3x and a Fable override under-counted 3x (AI-12).
_FAMILY_PRICING = (
    ("claude-opus-5-5", (4.00, 20.00)),
    ("claude-opus-5", (5.00, 25.00)),
    ("claude-opus-4", (5.00, 25.00)),
    ("claude-sonnet-5", (2.00, 10.00)),
    ("claude-sonnet-4", (3.00, 15.00)),
    ("claude-haiku-4", (1.00, 5.00)),
    ("claude-fable", (10.00, 50.00)),
    ("claude-mythos", (10.00, 50.00)),
)
# No family matched: price it like the most expensive family, so the budget
# over-counts an unknown model rather than letting it run under-counted.
_UNKNOWN_MODEL_PRICING = (10.00, 50.00)


def _price_for(model):
    if model in _MODEL_PRICING:
        return _MODEL_PRICING[model]
    m = (model or "").lower()
    if vendor_for(m) == "perplexity":
        # 'perplexity-search' / 'perplexity-sonar' (older rows) are sonar.
        name = m[len("perplexity-"):] if m.startswith("perplexity-") else m
        if name in ("search", ""):
            name = "sonar"
        return _PERPLEXITY_PRICING.get(name, _PERPLEXITY_UNKNOWN)
    for prefix, rates in _FAMILY_PRICING:
        if m.startswith(prefix):
            return rates
    return _UNKNOWN_MODEL_PRICING


# A request sent through the Message Batches API (ai_batches.py) is billed
# at half the list price — every token, the cache write and read rates
# included (AI cost audit 10/7/26 #19). Its ledger row is priced with this
# multiplier and stamped BATCH_PRICE_VERSION, so a cost report, the budgets
# and _reprice_legacy_rows (which only touches unversioned rows) all read it
# as what it cost; a synchronous call is never priced this way.
BATCH_PRICE_MULTIPLIER = 0.5
BATCH_PRICE_VERSION = PRICE_VERSION + "+batch"


def _estimate_cost(model, input_tokens, output_tokens,
                   cache_write_tokens=0, cache_read_tokens=0, rates=None, batch=False):
    in_rate, out_rate = rates or _price_for(model)
    cost = (((input_tokens or 0) / 1_000_000) * in_rate
            + ((output_tokens or 0) / 1_000_000) * out_rate
            + ((cache_write_tokens or 0) / 1_000_000) * in_rate * _CACHE_WRITE_MULTIPLIER
            + ((cache_read_tokens or 0) / 1_000_000) * in_rate * _cache_read_multiplier(model))
    return cost * BATCH_PRICE_MULTIPLIER if batch else cost


def _log_usage_safe(message, model, restaurant_id, action, latency_ms=None, attempts=None, call_id=None,
                    attribution=None):
    """Never let usage logging break the AI call it's measuring. The row's
    outcome is read from the message's stop_reason: a refusal or a
    max_tokens stop is billed like any answer but is not 'ok' (#52)."""
    try:
        usage = getattr(message, "usage", None)
        if usage is None:
            return None
        att = attribution or {}
        return log_ai_usage(
            restaurant_id, action or "unspecified", model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency_ms,
            outcome=outcome_of(message), stop_reason=getattr(message, "stop_reason", None),
            attempts=attempts, request_id=getattr(message, "_request_id", None) or getattr(message, "id", None),
            trigger=att.get("trigger"), actor_user_id=att.get("actor_user_id"),
            correlation_id=att.get("correlation_id"), call_id=call_id,
        )
    except Exception:
        return None


def _log_cut_safe(partial, model, restaurant_id, action, latency_ms, attempts, call_id, attribution):
    """The ledger row for a call cut at its caller's deadline
    (CallDeadlineExceeded): what had streamed is billed, so it is filed with
    its tokens as 'truncated', stop_reason 'deadline' — never as a free error
    the AI budget would not count (schedule audit 10/3/26 P-22)."""
    try:
        usage = getattr(partial, "usage", None)
        att = attribution or {}
        log_ai_usage(
            restaurant_id, action or "unspecified", model,
            getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency_ms, outcome="truncated", stop_reason="deadline", attempts=attempts,
            reason="timeout", request_id=getattr(partial, "id", None),
            trigger=att.get("trigger"), actor_user_id=att.get("actor_user_id"),
            correlation_id=att.get("correlation_id"), call_id=call_id)
    except Exception:
        pass


def _log_failure_safe(exc, model, restaurant_id, action, attempts=None, reason=None, latency_ms=None,
                      call_id=None, attribution=None):
    try:
        att = attribution or {}
        log_ai_usage(restaurant_id, action or "unspecified", model, 0, 0,
                     status="error", error=f"{type(exc).__name__}: {str(exc)[:400]}",
                     outcome="error", reason=reason or classify_error(exc), attempts=attempts,
                     latency_ms=latency_ms, call_id=call_id,
                     request_id=getattr(exc, "request_id", None),
                     trigger=att.get("trigger"), actor_user_id=att.get("actor_user_id"),
                     correlation_id=att.get("correlation_id"))
    except Exception:
        pass


_USAGE_INSERT_COLS = ("restaurant_id", "action", "model", "input_tokens", "output_tokens", "cost_usd",
                      "status", "error", "cache_write_tokens", "cache_read_tokens", "latency_ms",
                      "vendor", "outcome", "stop_reason", "attempts", "request_id", "trigger",
                      "actor_user_id", "correlation_id", "price_version", "call_id", "reason")
_USAGE_INSERT_SQL = ("INSERT INTO ai_usage (" + ", ".join(f'"{c}"' for c in _USAGE_INSERT_COLS) + ") VALUES ("
                     + ",".join("?" * len(_USAGE_INSERT_COLS)) + ")")


def log_ai_usage(restaurant_id, action, model, input_tokens, output_tokens, db_path=None,
                 status="ok", error=None, cache_write_tokens=0, cache_read_tokens=0,
                 latency_ms=None, vendor=None, outcome=None, stop_reason=None, attempts=None,
                 request_id=None, trigger=None, actor_user_id=None, correlation_id=None,
                 call_id=None, reason=None, cost_usd=None, batch=False):
    """One ledger row; returns its id. `outcome` defaults from the legacy
    `status`; the row's status is derived from the outcome, so every older
    reader of `status` still sees ok / error. Tokens are billed whatever the
    outcome — a refusal and a truncation cost what they cost — and a blocked
    row costs nothing. Unattributed calls are attributed here
    (ai_context / the request / the stack). `batch` prices the tokens at the
    Message Batches rate and stamps BATCH_PRICE_VERSION (ai_batches.py)."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    _ensure_usage_schema(conn, path)
    outcome = outcome or ("ok" if status == "ok" else ("blocked" if status == "blocked" else "error"))
    status = _status_for(outcome)
    vendor = vendor or vendor_for(model)
    if trigger is None:
        t, a, c = _attribution()
        trigger = t
        actor_user_id = actor_user_id if actor_user_id is not None else a
        correlation_id = correlation_id or c
    if cost_usd is None:
        billed = (input_tokens or output_tokens or cache_write_tokens or cache_read_tokens) and outcome != "blocked"
        cost_usd = (_estimate_cost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens,
                                   batch=batch)
                    if billed else 0.0)
    _row = (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status,
            (str(error)[:500] if error is not None else None), cache_write_tokens or 0, cache_read_tokens or 0,
            latency_ms, vendor, outcome, (str(stop_reason)[:40] if stop_reason else None), attempts,
            (str(request_id)[:120] if request_id else None), trigger, actor_user_id,
            (str(correlation_id)[:80] if correlation_id else None),
            BATCH_PRICE_VERSION if batch else PRICE_VERSION, call_id,
            (str(reason)[:60] if reason else None))
    try:
        try:
            cur = conn.execute(_USAGE_INSERT_SQL, _row)
        except sqlite3.OperationalError:
            # The table lost a column since this process last checked. Re-run
            # the migration and try once more rather than dropping the row —
            # see _ensure_usage_schema's note on schemas changing under a live
            # process.
            _ensure_usage_schema(conn, path, force=True)
            cur = conn.execute(_USAGE_INSERT_SQL, _row)
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()
    # Fold this call into the cached totals so a burst inside one cache
    # window still trips the ceiling rather than sliding under it.
    note_ai_spend(cost_usd, restaurant_id, vendor=vendor, trigger=trigger)
    return row_id


# A blocked call is one ledger row per (restaurant, action, reason) per
# minute, its `attempts` counting the refusals inside it: a loop that keeps
# hitting a stop shows up as a count, not as a thousand rows.
_BLOCKED_COALESCE_SECONDS = 60
_blocked_memo = {}
_blocked_lock = threading.Lock()


def log_blocked(restaurant_id, action, model, reason, detail=None, vendor=None, trigger=None,
                actor_user_id=None, correlation_id=None):
    """A call refused before it reached its provider (#48): budget,
    breaker, data_not_ready (the readiness gate), rate_limited, no_key,
    deadline (its job's wall clock had run out, schedule audit 10/3/26 P-22).
    Zero cost; `reason` is machine-readable, `detail` says which ceiling or
    why. Never raises."""
    try:
        import models
        vendor = vendor or vendor_for(model)
        key = (str(getattr(models, "DB_PATH", "")), restaurant_id, action, vendor, reason)
        now = time.time()
        with _blocked_lock:
            hit = _blocked_memo.get(key)
        if hit and now - hit[1] < _BLOCKED_COALESCE_SECONDS:
            conn = _conn()
            try:
                cur = conn.execute("UPDATE ai_usage SET attempts = COALESCE(attempts, 1) + 1 "
                                   "WHERE id=? AND outcome='blocked' AND reason=?", (hit[0], reason))
                conn.commit()
                if cur.rowcount == 1:
                    return hit[0]
            finally:
                conn.close()
        row_id = log_ai_usage(restaurant_id, action or "unspecified", model or vendor, 0, 0,
                              status="blocked", outcome="blocked", reason=reason,
                              error=(f"{reason}: {detail}" if detail else reason)[:300], attempts=1,
                              vendor=vendor, trigger=trigger, actor_user_id=actor_user_id,
                              correlation_id=correlation_id, cost_usd=0.0)
        with _blocked_lock:
            if len(_blocked_memo) > 2000:
                for k in [k for k, v in _blocked_memo.items() if now - v[1] > _BLOCKED_COALESCE_SECONDS]:
                    _blocked_memo.pop(k, None)
            _blocked_memo[key] = (row_id, now)
        return row_id
    except Exception as e:
        log.warning("blocked call not logged (%s/%s): %s", action, reason, e)
        return None


def mark_outcome(message_or_call_id, outcome, reason=None, db_path=None):
    """Re-file a call the caller found unusable after it returned — the
    reply did not parse, came back empty, or was cut off — against its
    ledger row and trace (#52). `message_or_call_id` is the message
    create_with_retry returned (it carries `_cavnar_call_id`) or the id.
    The cost stays: the tokens were billed. Never raises; False when there
    was nothing to mark (a test double, a call made before the ledger
    knew ids)."""
    call_id = (message_or_call_id if isinstance(message_or_call_id, str)
               else getattr(message_or_call_id, "_cavnar_call_id", None))
    if not call_id or outcome not in OUTCOMES:
        return False
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        try:
            cur = conn.execute("UPDATE ai_usage SET outcome=?, status=?, reason=COALESCE(?, reason) WHERE call_id=?",
                               (outcome, _status_for(outcome), (str(reason)[:60] if reason else None), call_id))
            try:
                conn.execute("UPDATE ai_calls SET outcome=? WHERE call_id=?", (outcome, call_id))
            except sqlite3.OperationalError:
                pass
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log.warning("mark_outcome(%s, %s) failed: %s", call_id, outcome, e)
        return False


class AIOutputRejected(ValueError):
    """The model answered but its output could not be used — it did not
    parse, was empty, or the checks refused it whole. A ValueError, so every
    handler that already treats a bad reply as a ValueError keeps doing so;
    its own type so a caller can file it as an AI-quality finding rather
    than a job failure (#58)."""


# ── Response Validation Layer log ───────────────────────────────────────────
# One row per validated model output (response_validation.log), beside
# ai_usage so catch rates join on the same `action`. The table is created at
# boot by models.init_db (never here). It holds NO answer and NO guest text:
# rule codes, counts, a hash of the original and each finding's offending
# token cut to 60 characters (an echo finding carries none). Since fix round
# G it carries the call_id of the model call it checked, when that call was
# made in the same context (#117).
VALIDATION_TOKEN_MAX = 60


def log_validation(restaurant_id, surface, action, verdict, rules=(), tokens=(), n_rewrites=0, n_drops=0,
                   n_caveats=0, text_hash="", mode="enforce", version="", db_path=None, call_id=None) -> None:
    from models import get_conn, DB_PATH
    if call_id is None:
        call_id = last_call_id(restaurant_id, action=action, surface=surface)
    row = (restaurant_id, str(surface or "")[:40], str(action or "")[:60], str(verdict or "")[:12],
           json.dumps(sorted(set(str(r) for r in rules or ()))),
           json.dumps([str(t)[:VALIDATION_TOKEN_MAX] for t in (tokens or ())][:20]),
           int(n_rewrites or 0), int(n_drops or 0), int(n_caveats or 0), str(text_hash or "")[:32],
           str(mode or "")[:10], str(version or "")[:16])
    conn = get_conn(db_path or DB_PATH)
    try:
        try:
            conn.execute(
                "INSERT INTO ai_validation_log (restaurant_id, surface, action, verdict, rules, tokens, n_rewrites, "
                "n_drops, n_caveats, text_hash, mode, version, call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row + (call_id,))
        except sqlite3.OperationalError:
            # A database booted before the call_id column (init_ai_ops adds it).
            conn.execute(
                "INSERT INTO ai_validation_log (restaurant_id, surface, action, verdict, rules, tokens, n_rewrites, "
                "n_drops, n_caveats, text_hash, mode, version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
    finally:
        conn.close()


def _percentile(values, pct):
    """Nearest-rank percentile of a list of numbers, or None."""
    import math
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(pct / 100.0 * len(vals)) - 1))
    return vals[k]


def usage_summary(restaurant_id=None, since_days=30, db_path=None):
    """Spend grouped by action+model, optionally scoped to one restaurant,
    over the last `since_days` days — most-expensive first. Latency is the
    p50 and p95 as well as the mean (one 12-million-ms outlier made the mean
    meaningless, AIOPS-20); outcome counts sit beside the calls."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    _ensure_usage_schema(conn, path)
    where = "WHERE created_at >= datetime('now', ?)"
    params = [f"-{since_days} days"]
    if restaurant_id is not None:
        where += " AND restaurant_id=?"
        params.append(restaurant_id)
    sql = f"""
        SELECT action, model, COUNT(*) as calls,
               SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
               SUM(COALESCE(cache_write_tokens,0)) as cache_write_tokens,
               SUM(COALESCE(cache_read_tokens,0)) as cache_read_tokens,
               ROUND(AVG(latency_ms)) as avg_latency_ms,
               MAX(latency_ms) as max_latency_ms,
               SUM(cost_usd) as cost_usd,
               SUM(CASE WHEN COALESCE(outcome, CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok' ELSE 'error' END)
                        <> 'ok' THEN 1 ELSE 0 END) AS not_ok
        FROM ai_usage {where}
        GROUP BY action, model ORDER BY cost_usd DESC
    """
    lat_sql = f"SELECT action, model, latency_ms FROM ai_usage {where} AND latency_ms IS NOT NULL"
    try:
        rows = conn.execute(sql, params).fetchall()
        lat = conn.execute(lat_sql, params).fetchall()
    except sqlite3.OperationalError:
        # Same self-healing path as the budget read: the schema can change
        # under a live process.
        _ensure_usage_schema(conn, path, force=True)
        rows = conn.execute(sql, params).fetchall()
        lat = conn.execute(lat_sql, params).fetchall()
    conn.close()
    by = {}
    for r in lat:
        by.setdefault((r["action"], r["model"]), []).append(r["latency_ms"])
    out = []
    for r in rows:
        d = dict(r)
        vals = by.get((d["action"], d["model"]), [])
        d["p50_latency_ms"] = _percentile(vals, 50)
        d["p95_latency_ms"] = _percentile(vals, 95)
        out.append(d)
    return out


# ── AI action rate limiting ─────────────────────────────────────────────────
# Client-facing endpoints that trigger an AI call on demand (regenerate draft,
# generate schedule, generate content, AI visibility check) had no limit on
# how often a restaurant could fire them — unlike auth_routes.py's IP-based
# login/2FA limiter. Same sliding-window approach, keyed by restaurant_id +
# action name instead of IP, so repeated clicks cost one restaurant's budget
# instead of silently being free to hammer.
#
# The window lives in the database (ai_rate_events) since fix round G (#96):
# a process-local dict meant a second gunicorn worker silently doubled every
# limit, which is one of the two things CLAUDE.md says blocks raising
# --workers. Each check is one short IMMEDIATE transaction; a row outlives
# its window by nothing (every check prunes its key's expired rows, and one
# check a minute prunes every key's). _ai_call_log is kept as the fallback
# when the table cannot be reached — bounded, with empty keys dropped
# (#151: the per-IP buckets used to grow for the life of the process).
_ai_call_log = {}
_ai_call_log_lock = threading.Lock()
_AI_CALL_LOG_MAX_KEYS = 5000
_rate_prune_state = {"at": 0.0}
_RATE_PRUNE_EVERY_SECONDS = 60


def _rate_bucket(key):
    """The limiter family a key belongs to ("regen", "aivis", "guestoptin")
    — what the console counts hits by. Never the key itself, which can hold
    a visitor's IP."""
    return str(key or "").split(":", 1)[0][:40] or "?"


def _memory_rate_limited(key, max_calls, window_secs, now):
    with _ai_call_log_lock:
        recent = [t for t in _ai_call_log.get(key, []) if now - t < window_secs]
        if len(recent) >= max_calls:
            _ai_call_log[key] = recent
            return True
        recent.append(now)
        _ai_call_log[key] = recent
        if len(_ai_call_log) > _AI_CALL_LOG_MAX_KEYS:
            # Drop every key whose newest entry is older than an hour (no
            # limiter here has a longer window), then, if still too many,
            # the oldest half.
            stale = [k for k, v in _ai_call_log.items() if not v or now - v[-1] > 3600]
            for k in stale:
                _ai_call_log.pop(k, None)
            if len(_ai_call_log) > _AI_CALL_LOG_MAX_KEYS:
                for k in sorted(_ai_call_log, key=lambda k: _ai_call_log[k][-1] if _ai_call_log[k] else 0)[
                        :len(_ai_call_log) // 2]:
                    _ai_call_log.pop(k, None)
        return False


def ai_rate_limited(key, max_calls=6, window_secs=60):
    """Return True if `key` has already made >= max_calls within window_secs.
    Records this call as having happened if not limited; a refused one is
    counted (ai_rate_hits, by bucket) so the console can say how often
    owners are told "too many" (#48)."""
    now = time.time()
    try:
        conn = _conn()
    except Exception:
        return _memory_rate_limited(key, max_calls, window_secs, now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM ai_rate_events WHERE key=? AND at > ?",
                         (key, now - window_secs)).fetchone()[0]
        if n >= max_calls:
            conn.execute("INSERT INTO ai_rate_hits (day, bucket, hits) VALUES (date('now'), ?, 1) "
                         "ON CONFLICT(day, bucket) DO UPDATE SET hits = hits + 1", (_rate_bucket(key),))
            conn.commit()
            return True
        conn.execute("DELETE FROM ai_rate_events WHERE key=? AND expires_at < ?", (key, now))
        conn.execute("INSERT INTO ai_rate_events (key, at, expires_at) VALUES (?,?,?)",
                     (key, now, now + window_secs))
        if now - _rate_prune_state["at"] > _RATE_PRUNE_EVERY_SECONDS:
            _rate_prune_state["at"] = now
            conn.execute("DELETE FROM ai_rate_events WHERE expires_at < ?", (now,))
        conn.commit()
        return False
    except sqlite3.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # No table (a database booted before init_ai_ops) or a locked one:
        # the process-local window still limits this process.
        log.debug("rate limiter fell back to memory: %s", e)
        return _memory_rate_limited(key, max_calls, window_secs, now)
    finally:
        conn.close()


def reset_rate_limits(db_path=None):
    """Clear every limiter window — the in-process fallback and the table.
    For tests and for an operator who needs a stuck owner unblocked."""
    with _ai_call_log_lock:
        _ai_call_log.clear()
    try:
        conn = _conn(db_path)
        try:
            conn.execute("DELETE FROM ai_rate_events")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("rate limit table not cleared: %s", e)


def extract_text(message) -> str:
    """First real text block from a Claude response. Every call site in this
    codebase used to do message.content[0].text directly, which assumes
    content[0] is text — true until a ThinkingBlock (or any other non-text
    block) shows up first, at which point it's an AttributeError instead of
    a response. create_with_retry() disables thinking by default so this
    should be redundant in practice, but it's the difference between a
    crash and a clean response if that ever changes upstream."""
    for block in message.content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""


class AIRefused(RuntimeError):
    """The model declined (stop_reason "refusal"). Not an answer, not an empty
    answer: extract_text returns "" for it, which every caller that saved the
    result used to store as a real, empty draft or reply (AI-24)."""


class DataNotReady(AIRefused):
    """The data a call rests on can't be stood on (data_health.readiness said
    refuse or wait), so the model was never called. An AIRefused, so every
    caller that already treats a refusal as "no answer, store nothing" does
    the same here; `readiness` carries the reason and any retry time."""

    def __init__(self, readiness):
        self.readiness = dict(readiness or {})
        why = self.readiness.get("reason") or "the data behind this isn't current"
        super().__init__(f"Not generated: {why}.")


def is_refusal(message) -> bool:
    return getattr(message, "stop_reason", None) == "refusal"


def is_platform_stop(exc) -> bool:
    """A stop that says nothing about the item being processed: the budget
    ceiling or the provider breaker. Per-item attempt caps must not count
    these, or one budget stop uses up every pending review's attempts and
    the reviews are never processed again (AI-4)."""
    return isinstance(exc, (AIBudgetExceeded, AIProviderDown))


def parse_json_reply(text, expect=None, accept=None, message=None):
    """The JSON value in a model's reply, tolerating what models actually add
    around it: a code fence, a leading "Here is the JSON:", a closing remark,
    or prose with brackets of its own (AI-26).

    `expect` is dict or list; `accept` an optional predicate the value must
    satisfy. The first value, scanning left to right, that parses and passes
    both is returned — so "[see note 1]" in a preamble is skipped rather than
    greedily joined to the real array. Raises ValueError when there is none;
    with `message` (what create_with_retry returned) the call is re-filed as
    'unparseable' in the ledger first (#52).
    """
    try:
        return _parse_json_reply(text, expect=expect, accept=accept)
    except ValueError:
        if message is not None:
            mark_outcome(message, "unparseable", reason="no usable JSON")
        raise


def _parse_json_reply(text, expect=None, accept=None):
    import json as _json
    raw = (text or "").strip()
    ok = lambda v: (expect is None or isinstance(v, expect)) and (accept is None or accept(v))
    try:
        val = _json.loads(raw)
        if ok(val):
            return val
    except ValueError:
        pass
    openers = "{[" if expect is None else ("{" if expect is dict else "[")
    decoder = _json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch not in openers:
            continue
        try:
            val, _end = decoder.raw_decode(raw, i)
        except ValueError:
            continue
        if ok(val):
            return val
    raise ValueError("the reply held no usable JSON")


class PlacesError(RuntimeError):
    """Google Places answered HTTP 200 with a non-OK `status`.

    Places reports a denied or unbilled key, an exhausted quota, a bad
    request and a Place ID that no longer exists as HTTP 200 with a status
    and no result, so raise_for_status never fires. Treating that body as
    "no reviews" recorded a fleet-wide ingestion stop as a successful quiet
    day, metered at full price, with the staleness check green. The message
    carries the status and Google's own error text, never the URL (which
    holds the key)."""

    def __init__(self, status, message=""):
        self.status = status or "UNKNOWN"
        text = f"Google Places {self.status}"
        if message:
            text += f": {str(message)[:200]}"
        super().__init__(text)

    @property
    def owner_message(self):
        if self.status == "NOT_FOUND" or self.status == "INVALID_REQUEST":
            return "Google no longer recognises this restaurant's listing ID — update it in Settings."
        return f"Google Places refused the request ({self.status}); it will be retried."


PLACES_OK_STATUSES = ("OK", "ZERO_RESULTS")


def places_error(body):
    """The PlacesError a Places response body stands for, or None when it is
    a real answer. A body without a status is left alone."""
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    if status is None or status in PLACES_OK_STATUSES:
        return None
    return PlacesError(status, body.get("error_message") or "")


def meter_places(restaurant_id, action, kind="details", status="ok", error=None, latency_ms=None,
                 reason=None, outcome=None):
    """Google Places is billed per request. Audit #7 found it outside the
    ledger and the budget entirely, so a Places-only restaurant's four daily
    review fetches and the weekly competitor run were real money that no
    ceiling could see. Best-effort: metering must never break a fetch.
    Was copied verbatim into fetcher, weather and competitor; one copy now,
    and since fix round G only places_request calls it — every Places
    request is metered as it is made, and against its own ceiling."""
    try:
        log_api_call(restaurant_id, action, f"google-places-{kind}",
                     calls=1, status=status, error=error, latency_ms=latency_ms, reason=reason,
                     outcome=outcome)
    except Exception:
        pass


# ══ AI operations: tables, trace, quality, health, rollup, Places ══════════
#
# Everything below is fix round G (9/29/26). The tables are created at boot
# (init_ai_ops, called from models.init_db) — never on a call path; a writer
# that meets a database without them logs and carries on.

_AI_OPS_DDL = (
    # One row per provider call: what was asked, of which model, why, and
    # what came back (#117). The prompt and output text are kept (redacted,
    # compressed) only for the last AI_TRACE_KEEP_PER_ACTION calls per
    # restaurant and action and for AI_TRACE_DAYS; the hashes, ids and stop
    # reason outlive them.
    """CREATE TABLE IF NOT EXISTS ai_calls (
        call_id        TEXT PRIMARY KEY,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        restaurant_id  INTEGER,
        action         TEXT,
        vendor         TEXT,
        model          TEXT,
        "trigger"      TEXT,
        actor_user_id  INTEGER,
        correlation_id TEXT,
        caller         TEXT,
        template_hash  TEXT,
        prompt_hash    TEXT,
        request_id     TEXT,
        stop_reason    TEXT,
        outcome        TEXT,
        output_hash    TEXT,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        latency_ms     INTEGER,
        attempts       INTEGER,
        usage_id       INTEGER,
        meta_json      TEXT,
        prompt_z       BLOB,
        output_z       BLOB
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_calls_rid ON ai_calls(restaurant_id, action, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_calls_created ON ai_calls(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_calls_corr ON ai_calls(correlation_id)",
    # Guard and validation findings, dropped lines, refused reads and served
    # fallbacks (#58, #140) — rates on the AI page, never "failed jobs".
    """CREATE TABLE IF NOT EXISTS ai_quality_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        restaurant_id  INTEGER,
        surface        TEXT NOT NULL,
        kind           TEXT NOT NULL,
        action         TEXT,
        detail         TEXT,
        codes          TEXT,
        n              INTEGER NOT NULL DEFAULT 1,
        call_id        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_quality_created ON ai_quality_events(created_at, surface)",
    "CREATE INDEX IF NOT EXISTS idx_ai_quality_rid ON ai_quality_events(restaurant_id, created_at)",
    # Breaker transitions, budget stops and warnings, credential / credit /
    # overload failures (#104) — cross-process, so /status and the console
    # read the same history whichever process saw it.
    """CREATE TABLE IF NOT EXISTS ai_health_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        vendor         TEXT NOT NULL,
        event          TEXT NOT NULL,
        scope          TEXT,
        restaurant_id  INTEGER,
        detail         TEXT,
        paged          INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_health_created ON ai_health_events(created_at, vendor)",
    # How much memory each prompt surface carries, per section and day, and
    # what the budget cut or a provider lost (memory re-audit 9/29/26,
    # PROMPTS-2/-10/-16): memory_context aggregates in-process and adds its
    # counts here every few minutes, so every process and every deploy lands
    # in one history the AI page reads. Pruned at AI_MEMORY_SIZES_RETAIN_DAYS
    # by prune_ai_ops. `day` is the UTC day.
    """CREATE TABLE IF NOT EXISTS ai_memory_sizes (
        day            TEXT NOT NULL,
        surface        TEXT NOT NULL,
        section        TEXT NOT NULL,
        calls          INTEGER NOT NULL DEFAULT 0,
        chars          INTEGER NOT NULL DEFAULT 0,
        max_chars      INTEGER NOT NULL DEFAULT 0,
        dropped        INTEGER NOT NULL DEFAULT 0,
        cut_calls      INTEGER NOT NULL DEFAULT 0,
        errors         INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (day, surface, section)
    )""",
    # The history the 120-day prune used to delete (#70): one row per UTC
    # day, restaurant, vendor, action and model, rebuilt from the raw rows
    # while they exist and never pruned. rid_key is restaurant_id with 0 for
    # unattributed, so the key is unique.
    """CREATE TABLE IF NOT EXISTS ai_usage_daily (
        day                TEXT NOT NULL,
        rid_key            INTEGER NOT NULL,
        restaurant_id      INTEGER,
        vendor             TEXT NOT NULL,
        action             TEXT NOT NULL,
        model              TEXT NOT NULL,
        calls              INTEGER NOT NULL DEFAULT 0,
        n_ok               INTEGER NOT NULL DEFAULT 0,
        n_error            INTEGER NOT NULL DEFAULT 0,
        n_refused          INTEGER NOT NULL DEFAULT 0,
        n_truncated        INTEGER NOT NULL DEFAULT 0,
        n_unparseable      INTEGER NOT NULL DEFAULT 0,
        n_blocked          INTEGER NOT NULL DEFAULT 0,
        attempts           INTEGER NOT NULL DEFAULT 0,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL NOT NULL DEFAULT 0,
        latency_n          INTEGER NOT NULL DEFAULT 0,
        latency_sum_ms     INTEGER NOT NULL DEFAULT 0,
        p50_ms             INTEGER,
        p95_ms             INTEGER,
        max_ms             INTEGER,
        admin_cost_usd     REAL NOT NULL DEFAULT 0,
        updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (day, rid_key, vendor, action, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_usage_daily_rid ON ai_usage_daily(rid_key, day)",
    # ai_validation_log's verdicts, per day and surface, kept past its prune.
    """CREATE TABLE IF NOT EXISTS ai_validation_daily (
        day        TEXT NOT NULL,
        rid_key    INTEGER NOT NULL,
        surface    TEXT NOT NULL,
        mode       TEXT NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0,
        n_pass     INTEGER NOT NULL DEFAULT 0,
        n_caveat   INTEGER NOT NULL DEFAULT 0,
        n_withhold INTEGER NOT NULL DEFAULT 0,
        n_refuse   INTEGER NOT NULL DEFAULT 0,
        rules_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (day, rid_key, surface, mode)
    )""",
    # The rate limiter's window (#96) and its refusals by bucket.
    """CREATE TABLE IF NOT EXISTS ai_rate_events (
        key        TEXT NOT NULL,
        at         REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_rate_events_key ON ai_rate_events(key, at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_rate_events_exp ON ai_rate_events(expires_at)",
    """CREATE TABLE IF NOT EXISTS ai_rate_hits (
        day    TEXT NOT NULL,
        bucket TEXT NOT NULL,
        hits   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, bucket)
    )""",
)


def init_ai_ops(db_path=None):
    """Boot DDL for the AI-operations tables and ai_usage's new columns, then
    the one-time data work (vendor backfill, the Sonnet 5 / Perplexity
    reprice, the rollup of history already in the ledger). Called from
    models.init_db. The data work never fails the boot."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    try:
        _ensure_usage_schema(conn, path, force=True)
        for sql in _AI_OPS_DDL:
            conn.execute(sql)
        # ai_validation_log (models.init_db's table) gains the call it checked.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_validation_log)").fetchall()}
        if cols and "call_id" not in cols:
            try:
                conn.execute("ALTER TABLE ai_validation_log ADD COLUMN call_id TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():    # another process added it first
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_action ON ai_usage(action, model)")
        conn.commit()
    finally:
        conn.close()
    for step in (_backfill_vendor, _reprice_legacy_rows, rollup_usage):
        try:
            step(db_path=path)
        except Exception as e:
            log.error("init_ai_ops: %s failed: %s", step.__name__, e)


def _backfill_vendor(db_path=None):
    """Every ledger row names its vendor (#68). Rows written before the
    column existed are classed by their model string."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        conn.execute("""UPDATE ai_usage SET vendor = CASE
                            WHEN COALESCE(model,'') LIKE 'google-places%' THEN 'google_places'
                            WHEN COALESCE(model,'') LIKE 'perplexity%' OR COALESCE(model,'') LIKE 'sonar%'
                                 OR COALESCE(action,'') = 'ai_visibility' THEN 'perplexity'
                            ELSE 'anthropic' END
                        WHERE vendor IS NULL""")
        conn.execute("""UPDATE ai_usage SET outcome = CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok'
                                                           WHEN status='blocked' THEN 'blocked'
                                                           ELSE 'error' END
                        WHERE outcome IS NULL""")
        conn.commit()
    finally:
        conn.close()


# What the ledger priced before a price_version existed: Sonnet 5 at the
# Sonnet-4 $3/$15 (to 9/22/26), and Perplexity's tokens — never a key in the
# table — at the Sonnet-4 rate before 9/22 and the unknown-model $10/$50 after.
_LEGACY_RATES = {"anthropic": ((3.00, 15.00),), "perplexity": ((3.00, 15.00), (10.00, 50.00))}


def _known_price(model):
    """Whether today's table prices `model` exactly or by its family — never
    the fail-safe unknown-model rate, which is a budget guard, not a price
    to rewrite history at."""
    m = (model or "").lower()
    if vendor_for(m) == "perplexity":
        return True
    return model in _MODEL_PRICING or any(m.startswith(prefix) for prefix, _r in _FAMILY_PRICING)


def _reprice_legacy_rows(db_path=None):
    """Once (#148): a row whose stored cost is exactly what an old wrong rate
    produced from its own tokens is repriced at today's rate and marked
    REPRICED_VERSION; every other unversioned row is marked 'legacy' as it
    stands. Self-verifying — a row is only changed when its cost proves which
    rate wrote it, and only to a rate today's table actually knows — so it
    is safe to run on every boot."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_usd "
            "FROM ai_usage WHERE price_version IS NULL AND COALESCE(status,'ok')='ok' AND cost_usd > 0 "
            "AND (COALESCE(model,'') LIKE 'claude%' OR COALESCE(model,'') LIKE 'perplexity%')").fetchall()
        fee = _PER_CALL_PRICING.get("perplexity-search", 0.0)
        updates = []
        for r in rows:
            args = (r["input_tokens"] or 0, r["output_tokens"] or 0,
                    r["cache_write_tokens"] or 0, r["cache_read_tokens"] or 0)
            stored = float(r["cost_usd"] or 0)
            model = r["model"] or ""
            if not _known_price(model):
                continue
            vendor = vendor_for(model)
            base = fee if vendor == "perplexity" else 0.0
            new = base + _estimate_cost(model, *args)
            for rates in _LEGACY_RATES.get(vendor, ()):
                old = base + _estimate_cost(model, *args, rates=rates)
                if abs(stored - old) <= 1e-9 + 1e-6 * old and abs(new - old) > 1e-9:
                    updates.append((round(new, 10), REPRICED_VERSION, r["id"]))
                    break
        if updates:
            conn.executemany("UPDATE ai_usage SET cost_usd=?, price_version=? WHERE id=?", updates)
            log.info("repriced %d ledger rows written at a superseded rate", len(updates))
        conn.execute("UPDATE ai_usage SET price_version='legacy' WHERE price_version IS NULL")
        conn.commit()
        return len(updates)
    finally:
        conn.close()


# ── the call trace (#117) ────────────────────────────────────────────────────
AI_TRACE_DAYS = int(os.getenv("AI_TRACE_DAYS", "30"))
AI_TRACE_KEEP_PER_ACTION = int(os.getenv("AI_TRACE_KEEP_PER_ACTION", "10"))
AI_TRACE_PROMPT_CHARS = int(os.getenv("AI_TRACE_PROMPT_CHARS", "40000"))
AI_TRACE_OUTPUT_CHARS = int(os.getenv("AI_TRACE_OUTPUT_CHARS", "12000"))
# Actions whose trace keeps more than the default: the week's schedule — a
# 60-person restaurant's prompt is 55-70k characters and its answer tens of
# thousands, so the 40k/12k caps cut every real week (schedule audit
# 10/3/26 PR-31). Still only the newest AI_TRACE_KEEP_PER_ACTION per
# restaurant keep their text; every schedule call's full input is also kept
# for replay in schedule_model_calls (schedule_output.record_call).
AI_TRACE_ACTION_CHARS = {
    "labor_schedule": (int(os.getenv("AI_TRACE_SCHEDULE_PROMPT_CHARS", "400000")),
                       int(os.getenv("AI_TRACE_SCHEDULE_OUTPUT_CHARS", "300000"))),
}


def _trace_caps(action):
    """(prompt chars, output chars) an action's trace keeps."""
    return AI_TRACE_ACTION_CHARS.get(action or "", (AI_TRACE_PROMPT_CHARS, AI_TRACE_OUTPUT_CHARS))


# The ai_calls rows themselves (hashes, ids, stop reason) — as long as the
# ledger rows they belong to.
AI_CALLS_RETAIN_DAYS = int(os.getenv("AI_CALLS_RETAIN_DAYS", "120"))

# Guest PII in a stored prompt: contact details anywhere, and the name on a
# labelled reviewer / guest line or JSON field. Staff names and the
# restaurant's own data are the business's, not a guest's.
_PII_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PII_PHONE_RE = re.compile(r"(?<![\w])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_PII_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PII_LABEL_RE = re.compile(r"(?im)^(\s*(?:reviewer|review author|author|guest|customer)(?:\s+name)?\s*[:=]\s*)(\S[^\n]{0,80})$")
_PII_JSON_RE = re.compile(r'("(?:author|author_name|reviewer|reviewer_name|guest_name|customer_name|'
                          r'guest|customer|to_email|phone|email)"\s*:\s*)"[^"]*"')


# A guest's name handed to a model on its own: the drafter fences the
# reviewer's name in an UNTRUSTED block of its own (a few capitalised words,
# no sentence), and the reply then greets them by it.
_PII_FENCED_NAME_RE = re.compile(r"<<<UNTRUSTED_GUEST_TEXT\n([A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,3})\nUNTRUSTED_GUEST_TEXT>>>")
_PII_JSON_NAME_RE = re.compile(r'"(?:author|author_name|reviewer|reviewer_name|guest_name|customer_name)"\s*:\s*"([^"]{2,60})"')


def guest_names_in(text):
    """The guest names a prompt carries — labelled lines, name fields and a
    name fenced on its own — so a trace can redact them in the output too."""
    t = str(text or "")
    names = set(m.group(2).strip() for m in _PII_LABEL_RE.finditer(t))
    names |= set(m.group(1).strip() for m in _PII_FENCED_NAME_RE.finditer(t))
    names |= set(m.group(1).strip() for m in _PII_JSON_NAME_RE.finditer(t))
    out = set()
    for n in names:
        if n and n.lower() not in ("guest", "anonymous", "a google user"):
            out.add(n)
            first = n.split()[0]
            if len(first) >= 3:
                out.add(first)          # "Hi Ann," greets the first name only
    return out


def redact_pii(text, names=()):
    """`text` with guest contact details and guest names replaced — labelled
    names, a name fenced on its own, and any of `names` wherever it appears —
    and anything credential-shaped removed (ai_guard.redact_secrets)."""
    t = str(text or "")
    try:
        from ai_guard import redact_secrets
        t = redact_secrets(t)
    except Exception:
        pass
    t = _PII_EMAIL_RE.sub("[email]", t)
    t = _PII_CARD_RE.sub("[number]", t)
    t = _PII_PHONE_RE.sub("[phone]", t)
    t = _PII_JSON_RE.sub(r'\1"[redacted]"', t)
    t = _PII_LABEL_RE.sub(r"\1[name]", t)
    t = _PII_FENCED_NAME_RE.sub("<<<UNTRUSTED_GUEST_TEXT\n[name]\nUNTRUSTED_GUEST_TEXT>>>", t)
    for n in sorted(set(names or ()), key=len, reverse=True):
        t = re.sub(r"(?<!\w)" + re.escape(n) + r"(?!\w)", "[name]", t)
    return t


def _block_text(block):
    """Readable text of one content block (dict or SDK object)."""
    get = (lambda k, d=None: block.get(k, d)) if isinstance(block, dict) else (lambda k, d=None: getattr(block, k, d))
    kind = get("type")
    if kind in (None, "text"):
        return str(get("text") or "")
    if kind == "tool_use":
        try:
            args = json.dumps(get("input"), default=str)[:800]
        except Exception:
            args = ""
        return f"[tool_use {get('name')} {args}]"
    if kind == "tool_result":
        content = get("content")
        if isinstance(content, list):
            return "[tool_result] " + "\n".join(_block_text(b) for b in content)
        return f"[tool_result] {content}"
    if kind in ("image", "document"):
        return f"[{kind}]"
    if kind == "thinking":
        return ""
    return f"[{kind}]"


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(_block_text(b) for b in content)
    return str(content or "")


def _prompt_text(kwargs):
    """The request as text: system, then each turn; tools by name only."""
    parts = []
    system = kwargs.get("system")
    if system:
        parts.append("[system]\n" + _content_text(system))
    for m in kwargs.get("messages") or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "?")
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        parts.append(f"[{role}]\n" + _content_text(content))
    tools = kwargs.get("tools")
    if tools:
        names = [t.get("name") if isinstance(t, dict) else getattr(t, "name", "?") for t in tools]
        parts.append(f"[tools: {len(names)}] " + ", ".join(str(n) for n in names[:60]))
    return "\n\n".join(parts)


def _static_system(kwargs):
    system = kwargs.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, (list, tuple)) and system:
        return _block_text(system[0])
    return ""


_template_cache = {}


def _caller_template(depth=3):
    """(caller, template_hash): the function that built the prompt — the
    frame calling create_with_retry — and a hash of its source, so a changed
    prompt template reads as a new version. Cached per code object."""
    try:
        f = sys._getframe(depth)
    except ValueError:
        return None, None
    code = f.f_code
    hit = _template_cache.get(code)
    if hit is None:
        import inspect
        mod = os.path.splitext(os.path.basename(code.co_filename or "?"))[0]
        try:
            src = inspect.getsource(code)
        except (OSError, TypeError):
            src = f"{code.co_filename}:{code.co_firstlineno}"
        hit = (f"{mod}.{code.co_name}", hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:16])
        if len(_template_cache) > 500:
            _template_cache.clear()
        _template_cache[code] = hit
    return hit


def _sha(text):
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _record_trace_safe(call_id, kwargs, message, restaurant_id, action, outcome, attribution,
                       attempts=None, latency_ms=None, usage_id=None):
    """One ai_calls row for a provider call. Never raises, never delays the
    caller by more than a compress and an insert."""
    try:
        caller, template = _caller_template(depth=3)
        prompt = _prompt_text(kwargs)
        static = _static_system(kwargs)
        template_hash = _sha(f"{template}:{_sha(static)}") if template else _sha(static)
        output = _content_text(getattr(message, "content", "")) if message is not None else ""
        names = guest_names_in(prompt)
        usage = getattr(message, "usage", None)
        att = attribution or {}
        prompt_cap, output_cap = _trace_caps(action)
        row = (call_id, restaurant_id, action or "unspecified", "anthropic", kwargs.get("model"),
               att.get("trigger"), att.get("actor_user_id"), att.get("correlation_id"), caller,
               template_hash, _sha(prompt),
               (getattr(message, "_request_id", None) if message is not None else None),
               (getattr(message, "stop_reason", None) if message is not None else None), outcome,
               _sha(output) if output else None,
               getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
               latency_ms, attempts, usage_id,
               zlib.compress(redact_pii(prompt[:prompt_cap], names).encode("utf-8", "replace")),
               zlib.compress(redact_pii(output[:output_cap], names).encode("utf-8", "replace"))
               if output else None)
        conn = _conn()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO ai_calls (call_id, restaurant_id, action, vendor, model, "trigger", '
                "actor_user_id, correlation_id, caller, template_hash, prompt_hash, request_id, stop_reason, "
                "outcome, output_hash, input_tokens, output_tokens, latency_ms, attempts, usage_id, prompt_z, "
                "output_z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            # Only the newest AI_TRACE_KEEP_PER_ACTION keep their text.
            conn.execute(
                "UPDATE ai_calls SET prompt_z=NULL, output_z=NULL WHERE call_id IN ("
                " SELECT call_id FROM ai_calls WHERE restaurant_id IS ? AND action=? AND prompt_z IS NOT NULL"
                " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (restaurant_id, action or "unspecified", max(0, AI_TRACE_KEEP_PER_ACTION)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("ai_calls trace not recorded (%s): %s", action, e)


def annotate_call(call_id, meta=None, db_path=None):
    """Attach a small JSON summary (Ask's tools, depth, confidence, verdict)
    to a traced call — what the client was shown alongside the answer."""
    if not call_id or not meta:
        return False
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        try:
            cur = conn.execute("UPDATE ai_calls SET meta_json=? WHERE call_id=?",
                               (json.dumps(meta, default=str)[:4000], call_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        log.debug("annotate_call failed: %s", e)
        return False


def read_call(call_id, db_path=None):
    """One traced call with its prompt and output text (when still kept),
    for the console. None when unknown."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute("SELECT * FROM ai_calls WHERE call_id=?", (call_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    out = {k: row[k] for k in row.keys() if k not in ("prompt_z", "output_z")}
    for src, dst in (("prompt_z", "prompt"), ("output_z", "output")):
        blob = row[src]
        try:
            out[dst] = zlib.decompress(blob).decode("utf-8", "replace") if blob else None
        except Exception:
            out[dst] = None
    try:
        out["meta"] = json.loads(out.pop("meta_json") or "null")
    except (TypeError, ValueError):
        out["meta"] = None
    out["text_kept"] = out.get("prompt") is not None
    return out


# ── AI quality events (#58) ──────────────────────────────────────────────────
QUALITY_KINDS = ("figures", "causes", "bindings", "names", "validation_refused", "line_dropped",
                 "item_dropped", "citation_dropped", "operational_evidence", "safety_disagreement",
                 "model_refused", "truncated", "unparseable", "fallback", "output_rejected")
AI_QUALITY_RETAIN_DAYS = int(os.getenv("AI_QUALITY_RETAIN_DAYS", "180"))


def record_quality_event(surface, kind, restaurant_id=None, detail=None, codes=None, n=1, action=None,
                         call_id=None, db_path=None):
    """A guard or validation finding, a dropped line, a refused read or a
    served fallback — what used to be an ops.capture into job_failures (and a
    Sentry event) and read as a failing job. Counted as a rate on the AI
    page. `detail` is redacted and cut to 300 characters; no guest text
    should be passed. Never raises; False when not recorded."""
    try:
        if call_id is None:
            call_id = last_call_id(restaurant_id, action=action or surface, surface=surface)
        conn = _conn(db_path)
        try:
            conn.execute("INSERT INTO ai_quality_events (restaurant_id, surface, kind, action, detail, codes, n, "
                         "call_id) VALUES (?,?,?,?,?,?,?,?)",
                         (restaurant_id, str(surface or "?")[:60], str(kind or "?")[:40],
                          (str(action)[:40] if action else None),
                          redact_pii(str(detail or ""))[:300] or None,
                          json.dumps(sorted(set(str(c) for c in codes))) if codes else None,
                          int(n or 1), call_id))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("AI quality event not recorded (%s/%s): %s", surface, kind, e)
        return False


def quality_counts(days=1, surface=None, kind=None, db_path=None):
    """{(surface, kind): n} over the last `days` days — the rate readers."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        sql = ("SELECT surface, kind, SUM(n) AS n FROM ai_quality_events WHERE created_at >= datetime('now', ?)")
        args = [f"-{int(days)} days"]
        if surface:
            sql += " AND surface=?"
            args.append(surface)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = conn.execute(sql + " GROUP BY surface, kind", args).fetchall()
        return {(r["surface"], r["kind"]): int(r["n"] or 0) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# ── AI health: events, paging, the status the public page shows (#104) ───────
AI_PAGE_COOLDOWN_MINUTES = int(os.getenv("AI_PAGE_COOLDOWN_MINUTES", "60"))
AI_HEALTH_RETAIN_DAYS = int(os.getenv("AI_HEALTH_RETAIN_DAYS", "365"))
# memory_context's per-surface section sizes (ai_memory_sizes): a year and a
# month, so a surface's memory can be compared with the same month last year.
AI_MEMORY_SIZES_RETAIN_DAYS = int(os.getenv("AI_MEMORY_SIZES_RETAIN_DAYS", "400"))


def record_health_event(vendor, event, scope=None, restaurant_id=None, detail=None, paged=False, db_path=None):
    """One row in ai_health_events. Never raises."""
    try:
        conn = _conn(db_path)
        try:
            cur = conn.execute("INSERT INTO ai_health_events (vendor, event, scope, restaurant_id, detail, paged) "
                               "VALUES (?,?,?,?,?,?)",
                               (str(vendor)[:30], str(event)[:40], (str(scope)[:120] if scope else None),
                                restaurant_id, (redact_pii(str(detail))[:400] if detail else None),
                                1 if paged else 0))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        log.warning("AI health event not recorded (%s %s): %s", vendor, event, e)
        return None


def _page(key, subject, lines):
    """Tell Will now (ops.alert_will: email + push), at most once per key per
    AI_PAGE_COOLDOWN_MINUTES, off the calling thread. Only where the
    scheduler runs (scheduler.scheduling_allowed) — a laptop pointed at
    production keys must not page anyone. Returns True when a page was
    started. Never raises."""
    try:
        import scheduler as _sched
        if not _sched.scheduling_allowed():
            return False
    except Exception:
        return False
    try:
        import ops
        if not ops.claim_cooldown(f"ai_page:{key}", AI_PAGE_COOLDOWN_MINUTES):
            return False
    except Exception as e:
        log.warning("AI page cooldown unavailable (%s): %s", key, e)
        return False

    def _go():
        try:
            import ops
            sent = ops.alert_will(subject, list(lines or [])[:12])
            record_health_event(key.split(":", 1)[-1] if ":" in key else "ai", "paged", scope=key,
                                detail=subject, paged=bool(sent))
        except Exception as e:
            log.error("AI page failed (%s): %s", key, e)
    threading.Thread(target=_go, daemon=True, name="ai-page").start()
    return True


def _vendor_key_configured(vendor):
    if vendor == "anthropic":
        return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    if vendor == "perplexity":
        return bool((os.getenv("PERPLEXITY_API_KEY") or "").strip())
    if vendor == "google_places":
        try:
            import config
            return bool(config.google_places_key())
        except Exception:
            return False
    return False


def ai_health(db_path=None):
    """The AI layer's health for the console, /status and paging:

        {"vendors": {vendor: {label, key_configured, breaker, breaker_seconds,
                              breaker_failures, last_breaker_event,
                              calls_1h, errors_1h, error_rate_1h,
                              blocked_1h, last_error, last_ok_at}},
         "last_auth_error": {vendor, event, detail, at} | None,
         "budget": {"global_month": {...}, "places_month": {...}},
         "status": "operational" | "degraded" | "outage", "reason": str | None,
         "events": [the 20 newest ai_health_events]}

    Error rate counts calls that reached the provider (ok, error, refused,
    truncated, unparseable); a provider error is outcome 'error'. The breaker
    is this process's; last_breaker_event is the persisted one, so a worker
    process reads what the web process saw."""
    from models import get_conn, DB_PATH
    out = {"vendors": {}, "last_auth_error": None, "events": []}
    brk = breakers()
    rows, events, last_ok = {}, [], {}
    conn = get_conn(db_path or DB_PATH)
    try:
        try:
            for r in conn.execute(
                    "SELECT COALESCE(vendor, 'anthropic') AS vendor, "
                    "SUM(CASE WHEN COALESCE(outcome,'ok') <> 'blocked' THEN 1 ELSE 0 END) AS calls, "
                    "SUM(CASE WHEN COALESCE(outcome, CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok' ELSE 'error' END)"
                    " = 'error' THEN 1 ELSE 0 END) AS errors, "
                    "SUM(CASE WHEN outcome='blocked' THEN COALESCE(attempts,1) ELSE 0 END) AS blocked "
                    "FROM ai_usage WHERE created_at >= datetime('now','-1 hour') GROUP BY 1").fetchall():
                rows[r["vendor"]] = dict(r)
            for r in conn.execute(
                    "SELECT COALESCE(vendor,'anthropic') AS vendor, MAX(created_at) AS at FROM ai_usage "
                    "WHERE COALESCE(outcome, CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok' ELSE 'error' END) "
                    "IN ('ok','refused','truncated','unparseable') AND created_at >= datetime('now','-7 days') "
                    "GROUP BY 1").fetchall():
                last_ok[r["vendor"]] = r["at"]
            last_err = {}
            for r in conn.execute(
                    "SELECT COALESCE(vendor,'anthropic') AS vendor, error, reason, created_at FROM ai_usage "
                    "WHERE id IN (SELECT MAX(id) FROM ai_usage WHERE outcome='error' "
                    "AND created_at >= datetime('now','-7 days') GROUP BY COALESCE(vendor,'anthropic'))").fetchall():
                last_err[r["vendor"]] = {"error": (r["error"] or "")[:200], "reason": r["reason"],
                                         "at": r["created_at"]}
        except sqlite3.Error:
            last_err = {}
        try:
            events = [dict(r) for r in conn.execute(
                "SELECT id, created_at, vendor, event, scope, restaurant_id, detail, paged FROM ai_health_events "
                "ORDER BY id DESC LIMIT 20").fetchall()]
            # Each vendor's newest breaker transition and credential failure,
            # however many other events came after them.
            last_events = {(r["vendor"], r["kind"]): dict(r) for r in conn.execute(
                "SELECT e.id, e.created_at, e.vendor, e.event, e.scope, e.detail, "
                "CASE WHEN e.event LIKE 'breaker_%' THEN 'breaker' ELSE 'auth' END AS kind "
                "FROM ai_health_events e WHERE e.id IN (SELECT MAX(id) FROM ai_health_events "
                "WHERE event LIKE 'breaker_%' OR event IN ('auth_error','credit_error','not_found') "
                "GROUP BY vendor, CASE WHEN event LIKE 'breaker_%' THEN 'breaker' ELSE 'auth' END)").fetchall()}
        except sqlite3.Error:
            events, last_events = [], {}
    finally:
        conn.close()
    for v in BREAKER_PROVIDERS:
        r = rows.get(v) or {}
        calls, errors = int(r.get("calls") or 0), int(r.get("errors") or 0)
        last_breaker = last_events.get((v, "breaker"))
        out["vendors"][v] = {
            "label": VENDOR_LABELS.get(v, v), "key_configured": _vendor_key_configured(v),
            "breaker": brk[v]["state"], "breaker_seconds": brk[v]["seconds_remaining"],
            "breaker_failures": brk[v]["failures"], "breaker_reason": brk[v]["reason"],
            "last_breaker_event": last_breaker, "last_auth_event": last_events.get((v, "auth")),
            "calls_1h": calls, "errors_1h": errors,
            "error_rate_1h": round(100.0 * errors / calls, 1) if calls else None,
            "blocked_1h": int(r.get("blocked") or 0),
            "last_error": last_err.get(v), "last_ok_at": last_ok.get(v),
        }
    auth_events = [e for (v, k), e in last_events.items() if k == "auth"]
    out["last_auth_error"] = max(auth_events, key=lambda e: e["id"]) if auth_events else None
    out["events"] = events
    try:
        g = ai_budget_status(None, db_path)["global_month"]
        p = places_budget_status(None, db_path)["month"]
        out["budget"] = {"global_month": g, "places_month": p}
    except Exception:
        out["budget"] = {}
    out["status"], out["reason"] = _service_status(out)
    return out


# The public status row's thresholds: over the last hour, at least this many
# calls reached the provider and this share of them failed.
AI_STATUS_MIN_CALLS = int(os.getenv("AI_STATUS_MIN_CALLS", "5"))
AI_STATUS_DEGRADED_PCT = float(os.getenv("AI_STATUS_DEGRADED_PCT", "20"))
AI_STATUS_OUTAGE_PCT = float(os.getenv("AI_STATUS_OUTAGE_PCT", "50"))


def _service_status(health):
    """(state, message) for the public "AI Review Drafting" row — worded for
    anyone, with no provider detail."""
    a = (health.get("vendors") or {}).get("anthropic") or {}
    if not a.get("key_configured"):
        return "outage", "API key not configured"
    auth = a.get("last_auth_event")
    if auth:
        last_ok = a.get("last_ok_at")
        if not last_ok or str(last_ok) < str(auth.get("created_at")):
            if _age_minutes(auth.get("created_at")) <= 60:
                return "outage", "AI drafting is unavailable — the provider is rejecting requests"
    if a.get("breaker") == "open":
        return "outage", "AI drafting is paused — the AI provider is not responding"
    lb = a.get("last_breaker_event")
    if lb and lb.get("event") == "breaker_open" and _age_minutes(lb.get("created_at")) <= 15:
        last_ok = a.get("last_ok_at")
        if not last_ok or str(last_ok) < str(lb.get("created_at")):
            return "outage", "AI drafting is paused — the AI provider is not responding"
    calls, rate = a.get("calls_1h") or 0, a.get("error_rate_1h")
    if calls >= AI_STATUS_MIN_CALLS and rate is not None:
        if rate >= AI_STATUS_OUTAGE_PCT:
            return "outage", f"Most AI requests are failing ({rate:.0f}% in the last hour)"
        if rate >= AI_STATUS_DEGRADED_PCT:
            return "degraded", f"Some AI requests are failing ({rate:.0f}% in the last hour)"
    g = ((health.get("budget") or {}).get("global_month")) or {}
    if g.get("over"):
        return "degraded", "AI features are paused while the platform budget resets"
    return "operational", None


def _age_minutes(stamp):
    """Minutes since a SQLite UTC stamp ('YYYY-MM-DD HH:MM:SS'); large when
    unreadable."""
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(str(stamp)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 10 ** 9


AI_BREAKER_STUCK_MINUTES = int(os.getenv("AI_BREAKER_STUCK_MINUTES", "15"))


def check_ai_alerts(db_path=None):
    """Page when a provider's breaker has STAYED open (#48): its newest
    persisted transition is an opening older than AI_BREAKER_STUCK_MINUTES
    and nothing has succeeded since. Run with the status checks (every
    scheduler tick); the page itself is cooled down like every AI page.
    Returns the vendors paged."""
    paged = []
    try:
        health = ai_health(db_path)
    except Exception as e:
        log.warning("check_ai_alerts: health unavailable: %s", e)
        return paged
    for vendor, info in (health.get("vendors") or {}).items():
        ev = info.get("last_breaker_event") or {}
        if ev.get("event") != "breaker_open":
            continue
        age = _age_minutes(ev.get("created_at"))
        last_ok = info.get("last_ok_at")
        if age < AI_BREAKER_STUCK_MINUTES or (last_ok and str(last_ok) >= str(ev.get("created_at"))):
            continue
        label = info.get("label") or vendor
        if _page(f"breaker_stuck:{vendor}", f"Cavnar AI: {label} has been failing for {int(age)} minutes",
                 [f"The {label} breaker opened {int(age)} minutes ago ({ev.get('detail') or ev.get('scope') or ''}) "
                  "and no call has succeeded since.",
                  "Check the provider's status page and the errors on Operations → AI."]):
            paged.append(vendor)
    return paged


def ai_service_status(db_path=None):
    """(state, message) for status_manager's AI row — the 1-hour error rate,
    the breaker, credential failures and the global budget, not key
    presence alone (#104)."""
    try:
        return _service_status(ai_health(db_path))
    except Exception as e:
        log.warning("ai_service_status unavailable: %s", e)
        if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
            return "outage", "API key not configured"
        return "operational", None


# ── the daily rollup and the AI tables' retention (#70) ──────────────────────

def rollup_usage(db_path=None, recent_days=2):
    """Rebuild ai_usage_daily (and ai_validation_daily) from the raw rows for
    the last `recent_days` UTC days plus any day that has raw rows but no
    rollup yet, then prune the AI-operations tables. Idempotent: a day is
    recomputed whole. Run at boot (init_ai_ops), by the nightly retention
    pass before it prunes a raw AI row (ops.prune_ledgers — a failed rollup
    keeps them), and safe to run any time. Returns {"days": n,
    "validation_days": n, "pruned": {...}}."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    out = {"days": 0, "validation_days": 0}
    try:
        days = _days_to_roll(conn, "ai_usage", "ai_usage_daily", recent_days)
        for day in days:
            _roll_usage_day(conn, day)
        out["days"] = len(days)
        try:
            vdays = _days_to_roll(conn, "ai_validation_log", "ai_validation_daily", recent_days)
            for day in vdays:
                _roll_validation_day(conn, day)
            out["validation_days"] = len(vdays)
        except sqlite3.Error as e:
            # Only a database that has no validation ledger yet is skipped. Any
            # other failure raises (memory re-audit 9/29/26, FORGET-11): it
            # was swallowed, the rollup read as clean, and ops' registry then
            # pruned ai_validation_log with no daily row for those days.
            if "no such table" not in str(e).lower():
                raise
            log.warning("validation rollup skipped: %s", e)
        conn.commit()
    finally:
        conn.close()
    out["pruned"] = prune_ai_ops(db_path=path)
    return out


def _days_to_roll(conn, raw, rolled, recent_days):
    have = {r[0] for r in conn.execute(f"SELECT DISTINCT day FROM {rolled}").fetchall()}
    raw_days = [r[0] for r in conn.execute(
        f"SELECT DISTINCT date(created_at) FROM {raw} WHERE created_at IS NOT NULL").fetchall() if r[0]]
    recent = {conn.execute("SELECT date('now', ?)", (f"-{int(i)} days",)).fetchone()[0]
              for i in range(max(1, recent_days))}
    return sorted(d for d in raw_days if d not in have or d in recent)


def _roll_usage_day(conn, day):
    rows = conn.execute(
        "SELECT restaurant_id, COALESCE(vendor,'anthropic') AS vendor, COALESCE(action,'unspecified') AS action, "
        "COALESCE(model,'unknown') AS model, "
        "COALESCE(outcome, CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok' ELSE 'error' END) AS outcome, "
        "COALESCE(attempts, 1) AS attempts, COALESCE(input_tokens,0) AS tin, COALESCE(output_tokens,0) AS tout, "
        "COALESCE(cache_write_tokens,0) AS cw, COALESCE(cache_read_tokens,0) AS cr, COALESCE(cost_usd,0) AS cost, "
        "latency_ms, COALESCE(\"trigger\",'') AS trig FROM ai_usage WHERE date(created_at)=?", (day,)).fetchall()
    groups = {}
    for r in rows:
        k = (r["restaurant_id"] or 0, r["restaurant_id"], r["vendor"], r["action"], r["model"])
        g = groups.setdefault(k, {"calls": 0, "attempts": 0, "tin": 0, "tout": 0, "cw": 0, "cr": 0, "cost": 0.0,
                                  "admin_cost": 0.0, "lat": [], **{f"n_{o}": 0 for o in OUTCOMES}})
        outcome = r["outcome"] if r["outcome"] in OUTCOMES else "error"
        # A blocked row stands for every refusal coalesced into it.
        weight = int(r["attempts"] or 1) if outcome == "blocked" else 1
        g["calls"] += weight
        g[f"n_{outcome}"] += weight
        g["attempts"] += int(r["attempts"] or 1)
        g["tin"] += r["tin"]; g["tout"] += r["tout"]; g["cw"] += r["cw"]; g["cr"] += r["cr"]
        g["cost"] += float(r["cost"] or 0)
        if r["trig"] == "admin":
            g["admin_cost"] += float(r["cost"] or 0)
        if r["latency_ms"] is not None and outcome != "blocked":
            g["lat"].append(int(r["latency_ms"]))
    conn.execute("DELETE FROM ai_usage_daily WHERE day=?", (day,))
    conn.executemany(
        "INSERT INTO ai_usage_daily (day, rid_key, restaurant_id, vendor, action, model, calls, n_ok, n_error, "
        "n_refused, n_truncated, n_unparseable, n_blocked, attempts, input_tokens, output_tokens, "
        "cache_write_tokens, cache_read_tokens, cost_usd, latency_n, latency_sum_ms, p50_ms, p95_ms, max_ms, "
        "admin_cost_usd, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        [(day, k[0], k[1], k[2], k[3], k[4], g["calls"], g["n_ok"], g["n_error"], g["n_refused"],
          g["n_truncated"], g["n_unparseable"], g["n_blocked"], g["attempts"], g["tin"], g["tout"], g["cw"],
          g["cr"], round(g["cost"], 8), len(g["lat"]), sum(g["lat"]), _percentile(g["lat"], 50),
          _percentile(g["lat"], 95), max(g["lat"]) if g["lat"] else None, round(g["admin_cost"], 8))
         for k, g in groups.items()])


def _roll_validation_day(conn, day):
    rows = conn.execute("SELECT restaurant_id, surface, COALESCE(mode,'?') AS mode, verdict, rules "
                        "FROM ai_validation_log WHERE date(created_at)=?", (day,)).fetchall()
    groups = {}
    for r in rows:
        k = (r["restaurant_id"] or 0, r["surface"] or "?", r["mode"])
        g = groups.setdefault(k, {"n": 0, "pass": 0, "caveat": 0, "withhold": 0, "refuse": 0, "rules": {}})
        g["n"] += 1
        if r["verdict"] in ("pass", "caveat", "withhold", "refuse"):
            g[r["verdict"]] += 1
        try:
            for code in set(json.loads(r["rules"] or "[]")):
                g["rules"][code] = g["rules"].get(code, 0) + 1
        except (TypeError, ValueError):
            pass
    conn.execute("DELETE FROM ai_validation_daily WHERE day=?", (day,))
    conn.executemany(
        "INSERT INTO ai_validation_daily (day, rid_key, surface, mode, n, n_pass, n_caveat, n_withhold, n_refuse, "
        "rules_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(day, k[0], k[1], k[2], g["n"], g["pass"], g["caveat"], g["withhold"], g["refuse"],
          json.dumps(g["rules"], sort_keys=True)) for k, g in groups.items()])


def prune_ai_ops(db_path=None):
    """Retention for the fix-round-G tables. ai_usage and ai_validation_log
    keep their own windows (models / ops registries); the rollups are never
    pruned. Returns {table: rows}."""
    from models import get_conn, DB_PATH
    out = {}
    conn = get_conn(db_path or DB_PATH)
    try:
        for sql, args, name in (
                ("UPDATE ai_calls SET prompt_z=NULL, output_z=NULL WHERE prompt_z IS NOT NULL "
                 "AND created_at < datetime('now', ?)", (f"-{AI_TRACE_DAYS} days",), "ai_calls_text"),
                ("DELETE FROM ai_calls WHERE created_at < datetime('now', ?)",
                 (f"-{AI_CALLS_RETAIN_DAYS} days",), "ai_calls"),
                ("DELETE FROM ai_quality_events WHERE created_at < datetime('now', ?)",
                 (f"-{AI_QUALITY_RETAIN_DAYS} days",), "ai_quality_events"),
                ("DELETE FROM ai_health_events WHERE created_at < datetime('now', ?)",
                 (f"-{AI_HEALTH_RETAIN_DAYS} days",), "ai_health_events"),
                ("DELETE FROM ai_rate_events WHERE expires_at < ?", (time.time(),), "ai_rate_events"),
                ("DELETE FROM ai_rate_hits WHERE day < date('now', '-180 days')", (), "ai_rate_hits"),
                ("DELETE FROM ai_memory_sizes WHERE day < date('now', ?)",
                 (f"-{max(35, AI_MEMORY_SIZES_RETAIN_DAYS)} days",), "ai_memory_sizes")):
            try:
                cur = conn.execute(sql, args)
                if cur.rowcount and cur.rowcount > 0:
                    out[name] = cur.rowcount
            except sqlite3.Error as e:
                log.debug("prune_ai_ops skipped %s: %s", name, e)
        conn.commit()
    finally:
        conn.close()
    return out


# ── Google Places: one metered, budget-checked request path (#123) ───────────
PLACES_BASE_URL = "https://maps.googleapis.com/maps/api/place/"
_PLACES_KIND = {"details": "details", "nearbysearch": "nearby", "textsearch": "textsearch"}
_UNSET = object()


class PlacesUnavailable(RuntimeError):
    """A Places request refused before it was sent: no key, the Places
    breaker is open, or the restaurant's Places ceiling is spent. `reason`
    is no_key | breaker | budget."""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)

    @property
    def owner_message(self):
        return str(self)


# Places actions the ceiling counts but never refuses: the review fetch is
# the product's core read and the only path an urgent review (a health or
# safety complaint) arrives by, and it is bounded by the scheduler's four
# claimed slots a day. A loop elsewhere must not hold it back until the
# day resets; the stop is still recorded (and warned on) when it is over.
PLACES_ESSENTIAL_ACTIONS = frozenset({"review_fetch"})


def places_request(endpoint, params, restaurant_id=_UNSET, action=None, timeout=10):
    """GET https://maps.googleapis.com/maps/api/place/<endpoint>/json — the
    one way this codebase calls Google Places.

    Before the request: the key must be present (fail fast — an empty key
    was sent anyway and billed nothing but a REQUEST_DENIED), the Places
    breaker must be closed, and the restaurant's Places ceiling unspent;
    any of the three raises PlacesUnavailable and leaves a blocked row.
    After it: exactly one ledger row, metered at its SKU's price when Google
    answered OK or ZERO_RESULTS and at nothing when it refused; a 401/403 or
    REQUEST_DENIED trips the breaker (the key is dead for every request); a
    timeout, 5xx or quota refusal counts toward it. Returns the
    requests.Response — the caller parses it as it always did.

    `restaurant_id`/`action` default to ai_context's (then the request
    session's) when not given, for the helpers that take no restaurant. An
    action in PLACES_ESSENTIAL_ACTIONS counts toward the ceiling but is
    never refused by it."""
    kind = _PLACES_KIND.get(endpoint, endpoint)
    rid = _context_restaurant() if restaurant_id is _UNSET else restaurant_id
    act = action or (_CTX.get() or {}).get("action") or "places"
    sku_model = f"google-places-{kind}"
    if not (params or {}).get("key"):
        log_blocked(rid, act, sku_model, "no_key", detail="GOOGLE_PLACES_API_KEY is not set", vendor="google_places")
        raise PlacesUnavailable("no_key", "Google Places isn't configured on this server.")
    over = places_budget_exceeded(rid)
    if over:
        _record_budget_stop(over, rid, vendor="google_places")
        if act not in PLACES_ESSENTIAL_ACTIONS:
            log_blocked(rid, act, sku_model, "budget", detail=over, vendor="google_places")
            raise PlacesUnavailable("budget", f"Google lookups are paused for this restaurant — its {over} is spent.")
    try:
        _breaker_check("google_places")
    except AIProviderDown:
        log_blocked(rid, act, sku_model, "breaker", detail="the Places breaker is open", vendor="google_places")
        raise PlacesUnavailable("breaker", _PLACES_DOWN_MESSAGE)
    import requests
    started = time.time()
    try:
        resp = requests.get(f"{PLACES_BASE_URL}{endpoint}/json", params=params, timeout=timeout)
    except Exception as e:
        reason = "timeout" if "timeout" in type(e).__name__.lower() else "connection"
        try:
            from ai_guard import safe_error
            err = safe_error(e)[:200]
        except Exception:
            err = type(e).__name__
        meter_places(rid, act, kind, status="error", error=err, reason=reason,
                     latency_ms=int((time.time() - started) * 1000))
        _breaker_record("google_places", False, reason=reason)
        raise
    latency_ms = int((time.time() - started) * 1000)
    code = getattr(resp, "status_code", 200)
    code = code if isinstance(code, int) else 200
    body_error = None
    try:
        body = resp.json()
    except Exception as e:
        body, body_error = None, e
    pstatus = body.get("status") if isinstance(body, dict) else None
    detail = (str(body.get("error_message") or pstatus or "")[:200] if isinstance(body, dict) else "")
    if code in (401, 403) or pstatus == "REQUEST_DENIED":
        meter_places(rid, act, kind, status="error", error=f"HTTP {code} {detail}".strip(), reason="auth",
                     latency_ms=latency_ms)
        _note_provider_error("google_places", "auth", RuntimeError(f"Google Places {pstatus or code}: {detail}"))
        trip_breaker("google_places", "auth")
    elif code >= 500 or code == 429 or pstatus in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"):
        reason = "rate_limit" if (code == 429 or pstatus == "OVER_QUERY_LIMIT") else "server"
        meter_places(rid, act, kind, status="error", error=f"HTTP {code} {detail}".strip(), reason=reason,
                     latency_ms=latency_ms)
        _breaker_record("google_places", False, reason=reason)
    elif code >= 400 or (pstatus is not None and pstatus not in PLACES_OK_STATUSES):
        # A fact about this request (a dead Place ID, a bad parameter): the
        # provider answered, so its health is not in question.
        meter_places(rid, act, kind, status="error", error=f"HTTP {code} {detail}".strip(),
                     reason=(pstatus or f"http_{code}").lower()[:40], latency_ms=latency_ms)
        _breaker_release_probe("google_places")
    else:
        meter_places(rid, act, kind, latency_ms=latency_ms)
        _breaker_record("google_places", True)
    return _PlacesResponse(resp, body, body_error)


class _PlacesResponse:
    """What places_request returns: the requests.Response, its JSON body
    parsed once (the helper had to read it to meter it). json() returns that
    body — or raises what parsing raised — and everything else is the
    response's own."""

    def __init__(self, resp, body, body_error=None):
        self._resp = resp
        self._body = body
        self._body_error = body_error

    def json(self):
        if self._body_error is not None:
            raise self._body_error
        return self._body

    def raise_for_status(self):
        return self._resp.raise_for_status()

    def __getattr__(self, name):
        return getattr(self._resp, name)


# ── test / operator reset ────────────────────────────────────────────────────

def reset_process_state(db_path=None):
    """Clear this process's AI-operations memory: breakers, budget cache,
    warning and blocked-row memos, the rate limiter (memory and table). For
    the test suite, where every test starts from ids 1, 2, 3 again."""
    reset_breaker()
    _budget_cache.clear()
    _warned_memo.clear()
    with _blocked_lock:
        _blocked_memo.clear()
    reset_rate_limits(db_path)
