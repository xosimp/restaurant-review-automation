"""
ai_utils.py — shared helpers for Claude API calls across the app.

Every module (analyser, drafter, competitor, labor, marketing, inventory,
reporter, client_api) was calling client.messages.create() directly with no
retry logic — a transient rate limit or timeout just silently dropped that
one item (a review never got analyzed, a draft never got written), with no
backoff and no second attempt. This wraps the call once so every caller gets
the same retry behavior instead of each reimplementing it inconsistently.
"""
import os
import sqlite3
import time
import anthropic

# Errors worth retrying — transient/server-side. NOT retried: BadRequestError,
# AuthenticationError, PermissionDeniedError, NotFoundError — those are
# caller mistakes or config problems that a retry will never fix.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)


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


def _paying_client_count(db_path=None):
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) c FROM restaurants "
            "WHERE LOWER(TRIM(COALESCE(billing_status,''))) IN ('active','internal')"
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
    everyone's share of it. A budget of 0 still disables the ceiling.
    """
    if not AI_GLOBAL_MONTHLY_BUDGET_USD:
        return 0.0
    return max(AI_GLOBAL_MONTHLY_BUDGET_USD,
               _paying_client_count(db_path) * AI_GLOBAL_PER_CLIENT_USD)

# Unpaid accounts — demos, prospects, anything not billing_status active or
# internal — get their own, much smaller ceilings, AND their spend is excluded
# from the global figure above.
#
# Audit #5 found the sharp edge: ai_budget_exceeded checks the global ceiling
# first, for every caller, so spend on non-paying accounts could exhaust it
# and the people who then saw "AI is over budget" were the paying clients.
# A demo account going stale, or a handful of prospect accounts left open,
# should never be able to take Ask Cavnar away from someone who pays for it.
AI_UNPAID_DAILY_BUDGET_USD = float(os.getenv("AI_UNPAID_DAILY_BUDGET_USD", "2"))
AI_UNPAID_MONTHLY_BUDGET_USD = float(os.getenv("AI_UNPAID_MONTHLY_BUDGET_USD", "25"))

# billing_status values that count as paying for budget purposes.
_PAID_BILLING_STATES = {"active", "internal"}


def _is_paid_account(restaurant_id, db_path=None):
    """Whether this restaurant draws on the paid budgets and the global pool.

    A row that does not exist is not a client, so it gets the unpaid ceilings.
    A lookup that ERRORS fails open to paid, matching what every other gate in
    this codebase does (subscription_allows_access, restaurant_has_module): a
    database hiccup must not quietly drop a paying client onto a $2 ceiling.
    restaurant_id always comes from an authenticated session, so there is no
    caller who benefits from the open direction."""
    if restaurant_id is None:
        return True   # global/system calls are not attributable to a client
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        row = conn.execute("SELECT billing_status FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        conn.close()
        if not row:
            return False
        return (row["billing_status"] or "").strip().lower() in _PAID_BILLING_STATES
    except Exception:
        return True

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


def _spend_since(sql_window, restaurant_id=None, db_path=None, paid_only=False):
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    try:
        _ensure_usage_schema(conn, path)
        where = "WHERE created_at >= ?"
        params = [sql_window]
        if restaurant_id is not None:
            where += " AND restaurant_id=?"
            params.append(restaurant_id)
        elif paid_only:
            # The global pool is what paying clients share. Spend on demo and
            # prospect accounts is bounded by its own ceilings and must not
            # count here, or those accounts can starve the people paying.
            where += (" AND (restaurant_id IS NULL OR restaurant_id IN "
                      "(SELECT id FROM restaurants WHERE LOWER(TRIM(COALESCE(billing_status,''))) IN ('active','internal')))")
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


def _cached_spend(cache_key, sql_window, restaurant_id, db_path, paid_only=False):
    now = time.time()
    hit = _budget_cache.get(cache_key)
    if hit and now - hit[0] < _BUDGET_CACHE_SECS:
        return hit[1]
    spend = _spend_since(sql_window, restaurant_id, db_path, paid_only=paid_only)
    _budget_cache[cache_key] = (now, spend)
    return spend


def ai_budget_status(restaurant_id=None, db_path=None):
    """Spend against each ceiling. Also what the admin console reads."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d 00:00:00")
    month = now.strftime("%Y-%m-01 00:00:00")
    paid = _is_paid_account(restaurant_id, db_path)
    out = {
        "global_month": {"spend": _cached_spend(("g", month), month, None, db_path, paid_only=True),
                         "budget": global_monthly_budget(db_path)},
    }
    if restaurant_id is not None:
        # An unpaid account is bounded by its own, much smaller ceilings and
        # is deliberately absent from the global figure above, so it cannot
        # exhaust the pool a paying client depends on.
        out["day"] = {"spend": _cached_spend((restaurant_id, day), day, restaurant_id, db_path),
                      "budget": AI_DAILY_BUDGET_USD if paid else AI_UNPAID_DAILY_BUDGET_USD}
        out["month"] = {"spend": _cached_spend((restaurant_id, month), month, restaurant_id, db_path),
                        "budget": AI_MONTHLY_BUDGET_USD if paid else AI_UNPAID_MONTHLY_BUDGET_USD}
        out["paid"] = paid
        if not paid:
            # An unpaid account is never refused for the global pool it does
            # not draw on.
            out["global_month"] = dict(out["global_month"], budget=0.0)
    for k, v in out.items():
        if not isinstance(v, dict):
            continue
        v["over"] = bool(v["budget"]) and v["spend"] >= v["budget"]
        v["pct"] = round((v["spend"] / v["budget"]) * 100, 1) if v["budget"] else 0.0
    return out


def ai_budget_exceeded(restaurant_id=None, db_path=None):
    """Which ceiling is blown, as a human-readable scope, or None.

    Fails OPEN: if the ledger can't be read, the call goes through. A budget
    check is a backstop against a runaway, and letting a broken query take
    every AI feature offline would be a worse outage than the overspend it's
    guarding against — the failure is captured so it doesn't stay invisible.
    """
    try:
        status = ai_budget_status(restaurant_id, db_path)
        unpaid = status.get("paid") is False
        for scope, label in (("global_month", "monthly budget across all clients"),
                             ("day", "daily budget for accounts that aren't on a paid plan" if unpaid else "daily budget"),
                             ("month", "monthly budget for accounts that aren't on a paid plan" if unpaid else "monthly budget")):
            entry = status.get(scope)
            if isinstance(entry, dict) and entry.get("over"):
                return label
        return None
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="ai_budget_check", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return None


def _record_budget_stop(scope, restaurant_id):
    """One operator alert per scope per day, not one per blocked call."""
    try:
        import ops
        from datetime import datetime, timezone
        period = f"{scope}:{restaurant_id}:{datetime.now(timezone.utc):%Y-%m-%d}"
        if ops.claim_period("ai_budget_stop", period):
            ops.capture(AIBudgetExceeded(f"AI {scope} reached (restaurant_id={restaurant_id})"),
                        job="ai_budget", context=f"restaurant_id={restaurant_id}")
    except Exception:
        pass


def note_ai_spend(cost_usd, restaurant_id=None):
    """Keep the cached totals honest between refreshes, so a burst inside one
    cache window still trips the ceiling."""
    if not cost_usd:
        return
    for key in list(_budget_cache):
        if key[0] in ("g", restaurant_id):
            ts, spend = _budget_cache[key]
            _budget_cache[key] = (ts, spend + cost_usd)


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
    return fallback, 502


def create_with_retry(client, retries=2, backoff=1.5, restaurant_id=None, action=None, **kwargs):
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
    API; they're popped off before reaching client.messages.create()."""
    kwargs.setdefault("thinking", {"type": "disabled"})
    # anthropic>=0.105 (what Railway installs) rejects `temperature` outright
    # — TypeError before the request is even made — and current Sonnet
    # models refuse it server-side anyway. Strip it here so no caller can
    # take production down with a parameter that never mattered.
    kwargs.pop("temperature", None)
    over = ai_budget_exceeded(restaurant_id)
    if over:
        _record_budget_stop(over, restaurant_id)
        raise AIBudgetExceeded(
            f"AI is paused — this account has reached its {over}. "
            "Contact will@cavnar.ai if this looks wrong."
        )
    attempt = 0
    while True:
        try:
            # Wall time for the call itself, so "is Ask slow?" has an answer.
            # Nothing measured latency anywhere, and it matters more now that
            # an executive answer can run several tool rounds: a slow action
            # shows up here as a number rather than as a client's complaint.
            _started = time.time()
            message = client.messages.create(**kwargs)
            _log_usage_safe(message, kwargs.get("model", "unknown"), restaurant_id, action,
                            latency_ms=int((time.time() - _started) * 1000))
            return message
        except _RETRYABLE as e:
            attempt += 1
            if attempt > retries:
                # Retry budget exhausted — this is the "AI is down" signal the
                # operator digest exists for, so record it before re-raising.
                _log_failure_safe(e, kwargs.get("model", "unknown"), restaurant_id, action)
                try:
                    import ops
                    ops.capture(e, job="ai_call",
                                context=str(kwargs.get("model", "unknown")))
                except Exception:
                    pass
                raise
            time.sleep(backoff ** attempt)
        except Exception as e:
            # Not retryable (bad request, auth, a malformed response) — still
            # a failed AI call the admin console should see next to the
            # successes, in the same table.
            _log_failure_safe(e, kwargs.get("model", "unknown"), restaurant_id, action)
            raise


# ── AI cost/usage tracking ──────────────────────────────────────────────────
# Dozens of call sites across analyser/drafter/competitor/labor/marketing/
# inventory/reporter call Claude with zero visibility into what any of it
# costs — no way to see per-restaurant spend as client count grows, or
# whether one client's usage pattern (e.g. mashing "Regenerate") is eating
# margin. Every create_with_retry() call now logs here on success.
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
    error TEXT
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
    that the schema is immutable.
    """
    if db_path in _usage_schema_ready and not force:
        return
    try:
        conn.execute(_USAGE_TABLE_SQL)
        for _ix in _USAGE_INDEX_SQL:
            conn.execute(_ix)
        _ensure_usage_columns(conn)
        conn.commit()
        _usage_schema_ready.add(db_path)
    except Exception:
        # Leave it unmarked so the next call retries rather than reading a
        # table that was never created.
        pass


def _ensure_usage_columns(conn):
    """status/error arrived after the table existed on Railway, and the two
    cache columns after that — add them in place so old rows keep their
    (implicit) 'ok' and their implicit zero cache usage."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_usage)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN status TEXT DEFAULT 'ok'")
        if "error" not in cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN error TEXT")
        if "cache_write_tokens" not in cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN cache_write_tokens INTEGER DEFAULT 0")
        if "cache_read_tokens" not in cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN cache_read_tokens INTEGER DEFAULT 0")
        if "latency_ms" not in cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN latency_ms INTEGER")
    except Exception:
        pass

# $ per million tokens (input, output). Anthropic pricing as of this writing —
# update here if it changes; unknown models fall back to Sonnet-tier pricing
# so a forgotten update under-tracks rather than crashes.
_MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    # invoices.py reads prices off photos with the most capable model.
    "claude-opus-5": (5.00, 25.00),
    # Perplexity sonar, per million tokens. Audit #7 found this vendor was
    # entirely outside the ledger and the budget — the $10/day and
    # $1,500/month ceilings bound Claude only, while AI visibility could fire
    # nine sonar queries a minute per restaurant, unmetered and unlogged.
    "perplexity-sonar": (1.00, 1.00),
}

# Vendors billed per REQUEST rather than per token. Same ledger, same budget,
# so one ceiling covers every paid dependency instead of just the one that
# happened to have a token count.
#
# Rates are list prices at the time of writing and are the thing most likely
# to drift here — they are env-overridable so a price change is a Railway
# variable, not a deploy.
_PER_CALL_PRICING = {
    # Perplexity charges a per-search fee on top of tokens.
    "perplexity-search":     float(os.getenv("PRICE_PERPLEXITY_SEARCH", "0.005")),
    # Google Places Details / Nearby Search, roughly $17 per 1,000.
    "google-places-details": float(os.getenv("PRICE_PLACES_DETAILS", "0.017")),
    "google-places-nearby":  float(os.getenv("PRICE_PLACES_NEARBY", "0.032")),
}


def log_api_call(restaurant_id, action, vendor, calls=1, input_tokens=0, output_tokens=0,
                 db_path=None, status="ok", error=None):
    """Record a non-Claude paid dependency against the same budget.

    Perplexity and Google Places were both invisible to ai_budget_exceeded, so
    a runaway on either could not be stopped by the ceiling that exists to
    stop runaways. They land in ai_usage now, which means the admin console's
    spend view, the per-restaurant caps and the global pool all cover them.
    """
    per_call = _PER_CALL_PRICING.get(vendor, 0.0) * max(0, int(calls or 0))
    token_cost = _estimate_cost(vendor, input_tokens, output_tokens) if (input_tokens or output_tokens) else 0.0
    cost = 0.0 if status != "ok" else (per_call + token_cost)
    try:
        from models import get_conn, DB_PATH
        path = db_path or DB_PATH
        conn = get_conn(path)
        _ensure_usage_schema(conn, path)
        conn.execute(
            "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, status, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, action, vendor, input_tokens, output_tokens, cost, status, error),
        )
        conn.commit()
        conn.close()
        note_ai_spend(cost, restaurant_id)
    except Exception as e:
        log.warning("log_api_call(%s/%s) failed: %s", vendor, action, e)
    return cost


# Prompt caching is billed at its own rates, as a multiple of the model's
# input rate: writing a cache entry costs 1.25x, reading one 0.1x. Neither is
# included in `input_tokens`, which counts only the uncached remainder.
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


def _estimate_cost(model, input_tokens, output_tokens,
                   cache_write_tokens=0, cache_read_tokens=0):
    in_rate, out_rate = _MODEL_PRICING.get(model, (3.00, 15.00))
    return ((input_tokens / 1_000_000) * in_rate
            + (output_tokens / 1_000_000) * out_rate
            + (cache_write_tokens / 1_000_000) * in_rate * _CACHE_WRITE_MULTIPLIER
            + (cache_read_tokens / 1_000_000) * in_rate * _CACHE_READ_MULTIPLIER)


def _log_usage_safe(message, model, restaurant_id, action, latency_ms=None):
    """Never let usage logging break the AI call it's measuring."""
    try:
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        log_ai_usage(
            restaurant_id, action or "unspecified", model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
    except Exception:
        pass


def _log_failure_safe(exc, model, restaurant_id, action):
    try:
        log_ai_usage(restaurant_id, action or "unspecified", model, 0, 0,
                     status="error", error=f"{type(exc).__name__}: {str(exc)[:400]}")
    except Exception:
        pass


def log_ai_usage(restaurant_id, action, model, input_tokens, output_tokens, db_path=None,
                 status="ok", error=None, cache_write_tokens=0, cache_read_tokens=0,
                 latency_ms=None):
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    conn = get_conn(path)
    _ensure_usage_schema(conn, path)
    cost = (_estimate_cost(model, input_tokens, output_tokens,
                           cache_write_tokens, cache_read_tokens)
            if status == "ok" else 0.0)
    _row = (restaurant_id, action, model, input_tokens, output_tokens, cost, status, error,
            cache_write_tokens or 0, cache_read_tokens or 0, latency_ms)
    _insert = ("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, "
               "cost_usd, status, error, cache_write_tokens, cache_read_tokens, latency_ms) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)")
    try:
        conn.execute(_insert, _row)
    except sqlite3.OperationalError:
        # The table lost a column since this process last checked. Re-run the
        # migration and try once more rather than dropping the row — see
        # _ensure_usage_schema's note on schemas changing under a live process.
        _ensure_usage_schema(conn, path, force=True)
        conn.execute(_insert, _row)
    conn.commit()
    conn.close()
    # Fold this call into the cached totals so a burst inside one cache
    # window still trips the ceiling rather than sliding under it.
    note_ai_spend(cost, restaurant_id)


def usage_summary(restaurant_id=None, since_days=30, db_path=None):
    """Spend grouped by action+model, optionally scoped to one restaurant,
    over the last `since_days` days — most-expensive first."""
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
               SUM(cost_usd) as cost_usd
        FROM ai_usage {where}
        GROUP BY action, model ORDER BY cost_usd DESC
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # Same self-healing path as the budget read: the schema can change
        # under a live process.
        _ensure_usage_schema(conn, path, force=True)
        rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── AI action rate limiting ─────────────────────────────────────────────────
# Client-facing endpoints that trigger an AI call on demand (regenerate draft,
# generate schedule, generate content, AI visibility check) had no limit on
# how often a restaurant could fire them — unlike auth_routes.py's IP-based
# login/2FA limiter. Same sliding-window approach, keyed by restaurant_id +
# action name instead of IP, so repeated clicks cost one restaurant's budget
# instead of silently being free to hammer.
_ai_call_log = {}


def ai_rate_limited(key, max_calls=6, window_secs=60):
    """Return True if `key` has already made >= max_calls within window_secs.
    Records this call as having happened if not limited."""
    now = time.time()
    recent = [t for t in _ai_call_log.get(key, []) if now - t < window_secs]
    if len(recent) >= max_calls:
        _ai_call_log[key] = recent
        return True
    recent.append(now)
    _ai_call_log[key] = recent
    return False


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
