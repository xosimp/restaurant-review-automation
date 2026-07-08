"""
ai_utils.py — shared helpers for Claude API calls across the app.

Every module (analyser, drafter, competitor, labor, marketing, inventory,
reporter, client_api) was calling client.messages.create() directly with no
retry logic — a transient rate limit or timeout just silently dropped that
one item (a review never got analyzed, a draft never got written), with no
backoff and no second attempt. This wraps the call once so every caller gets
the same retry behavior instead of each reimplementing it inconsistently.
"""
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
    attempt = 0
    while True:
        try:
            message = client.messages.create(**kwargs)
            _log_usage_safe(message, kwargs.get("model", "unknown"), restaurant_id, action)
            return message
        except _RETRYABLE as e:
            attempt += 1
            if attempt > retries:
                # Retry budget exhausted — this is the "AI is down" signal the
                # operator digest exists for, so record it before re-raising.
                try:
                    import ops
                    ops.capture(e, job="ai_call",
                                context=str(kwargs.get("model", "unknown")))
                except Exception:
                    pass
                raise
            time.sleep(backoff ** attempt)


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
    cost_usd REAL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

# $ per million tokens (input, output). Anthropic pricing as of this writing —
# update here if it changes; unknown models fall back to Sonnet-tier pricing
# so a forgotten update under-tracks rather than crashes.
_MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}


def _estimate_cost(model, input_tokens, output_tokens):
    in_rate, out_rate = _MODEL_PRICING.get(model, (3.00, 15.00))
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _log_usage_safe(message, model, restaurant_id, action):
    """Never let usage logging break the AI call it's measuring."""
    try:
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        log_ai_usage(
            restaurant_id, action or "unspecified", model,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
    except Exception:
        pass


def log_ai_usage(restaurant_id, action, model, input_tokens, output_tokens, db_path=None):
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    conn.execute(_USAGE_TABLE_SQL)
    cost = _estimate_cost(model, input_tokens, output_tokens)
    conn.execute(
        "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd) VALUES (?,?,?,?,?,?)",
        (restaurant_id, action, model, input_tokens, output_tokens, cost),
    )
    conn.commit()
    conn.close()


def usage_summary(restaurant_id=None, since_days=30, db_path=None):
    """Spend grouped by action+model, optionally scoped to one restaurant,
    over the last `since_days` days — most-expensive first."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    conn.execute(_USAGE_TABLE_SQL)
    where = "WHERE created_at >= datetime('now', ?)"
    params = [f"-{since_days} days"]
    if restaurant_id is not None:
        where += " AND restaurant_id=?"
        params.append(restaurant_id)
    rows = conn.execute(f"""
        SELECT action, model, COUNT(*) as calls,
               SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens,
               SUM(cost_usd) as cost_usd
        FROM ai_usage {where}
        GROUP BY action, model ORDER BY cost_usd DESC
    """, params).fetchall()
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
