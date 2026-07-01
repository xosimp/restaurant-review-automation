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


def create_with_retry(client, retries=2, backoff=1.5, **kwargs):
    """client.messages.create(**kwargs) with exponential backoff on
    transient failures. Raises the last exception if all attempts fail."""
    attempt = 0
    while True:
        try:
            return client.messages.create(**kwargs)
        except _RETRYABLE as e:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(backoff ** attempt)


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
