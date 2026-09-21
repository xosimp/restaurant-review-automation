"""The privacy floor every cross-restaurant answer stands on.

Three mechanisms, all deliberately dumb enough to audit by reading:

* `MIN_COHORT` — no aggregate leaves the engine over fewer restaurants.
* `assert_anonymous()` — a recursive check that a payload bound for a
  cross-restaurant surface carries none of the keys that identify a
  restaurant, a person or an exact figure. Tests call it on every
  pattern, benchmark, trend and dashboard payload.
* `round_effect()` — effects are rounded so a difference of aggregates
  cannot be reversed into one member's value.
"""

MIN_COHORT = 5            # restaurants in a cohort before anything aggregate is said
MIN_GROUP = 5             # restaurants on each side of a behaviour split
MIN_WEEKS_FOR_TREND = 6   # weekly points before a slope means anything

# Keys that may never appear in a cross-restaurant payload. Substring
# match on the lower-cased key, so `owner_email`, `employee_name` and
# `google_place_id` are all caught by their stems.
FORBIDDEN_KEY_STEMS = (
    "restaurant_id", "restaurant_name", "owner", "email", "phone", "address",
    "employee", "staff_name", "customer", "guest", "place_id", "guid",
    "sales_total", "revenue", "net_sales", "total_sales", "labor_cost",
    "cogs", "dollars", "competitor", "username", "password", "token",
)

# Keys that look forbidden but are aggregates by construction.
ALLOWED_KEYS = {
    "n", "n_with", "n_without", "restaurants", "cohort", "cohorts",
    "dollars_monthly_median",   # a cohort median of measured outcomes
}


class PrivacyError(ValueError):
    pass


def cohort_ok(n) -> bool:
    try:
        return int(n or 0) >= MIN_COHORT
    except (TypeError, ValueError):
        return False


def round_effect(x, places=2):
    if x is None:
        return None
    try:
        return round(float(x), places)
    except (TypeError, ValueError):
        return None


def assert_anonymous(payload, path="payload"):
    """Raise PrivacyError if any key in `payload` (recursively) identifies a
    restaurant, a person or an exact figure. Returns the payload so it can
    be used inline: `return assert_anonymous(build())`."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = str(k).lower()
            if key not in ALLOWED_KEYS and any(stem in key for stem in FORBIDDEN_KEY_STEMS):
                raise PrivacyError(f"{path}.{k} must not leave the engine")
            assert_anonymous(v, f"{path}.{k}")
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            assert_anonymous(v, f"{path}[{i}]")
    return payload


def strip_identity(row: dict) -> dict:
    """The safe subset of a features row for cross-restaurant use: the
    features themselves and the cohort, never the restaurant id."""
    return {"cohort": row.get("cohort"), "week": row.get("week"),
            "features": dict(row.get("features") or {}), "completeness": row.get("completeness")}
