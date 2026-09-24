"""The privacy floor every cross-restaurant answer stands on.

Three mechanisms, all deliberately dumb enough to audit by reading:

* `MIN_COHORT` — no aggregate leaves the engine over fewer restaurants.
* `assert_anonymous()` — a recursive check that a payload bound for a
  cross-restaurant surface carries none of the keys that identify a
  restaurant, a person or an exact figure, and no string value carrying a
  tenant's name, a restaurant id or an email address. Tests call it on
  every pattern, benchmark, trend and dashboard payload.
* `round_effect()` — stored effects are rounded. Rounding alone does NOT
  stop a quartile of five values from being one member's exact figure
  (NS4 M6); what a restaurant is SHOWN goes through
  benchmarks.published(), which leaves the viewer's own row out, needs
  MIN_QUARTILE_N others and rounds to a coarse per-metric step.
"""

import re

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


# String VALUES are scanned too (NS6 §B finding 2): a payload whose keys
# were clean passed with {'note': "Simple EJ's runs 31%"}. Three shapes:
# a tenant's name, a tenant id written out, an email address.
_TENANT_ID_RE = re.compile(r"\b(?:restaurant|tenant|location|rid)(?:[\s_-]*id)?\s*[#:=]\s*\d+\b|"
                           r"\brestaurant_id\s*\d+\b", re.I)
_EMAIL_IN_VALUE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Names too generic to be read as a disclosure when they appear in a
# label or a sentence ("Pizza", "Other", "Bar").
_GENERIC_NAMES = {"other", "bar", "pub", "cafe", "café", "grill", "kitchen", "pizza", "tavern", "bistro",
                  "restaurant", "diner", "eatery", "lounge", "cantina", "demo", "test", "sample",
                  # the platform's own name appears in every cohort label
                  # ("Pizza on Cavnar"), so an internal account named after
                  # it must not make every label a disclosure
                  "cavnar", "cavnar ai", "cavnar demo", "cavnar test"}
_NAMES_TTL_SECONDS = 60
_names_cache = {"key": None, "at": 0.0, "rx": None}


def _names_regex(names):
    keep = sorted({n.strip() for n in names or () if n and len(n.strip()) >= 4
                   and n.strip().lower() not in _GENERIC_NAMES}, key=len, reverse=True)
    if not keep:
        return None
    alt = "|".join(re.escape(n) for n in keep)
    return re.compile(r"(?<![\w'])(?:" + alt + r")(?![\w'])", re.I)


def invalidate_tenant_names(*_a):
    _names_cache.update({"key": None, "at": 0.0, "rx": None})


def _tenant_names_regex():
    """Every tenant's name as one pattern, cached per process for
    _NAMES_TTL_SECONDS (and dropped on a restaurant change). Read lazily
    from the data layer — this module stays L0. A failed read scans no
    names rather than raising inside a payload build (the key check and the
    id / email checks still run)."""
    import time as _time
    try:
        import models as _m
        key = (getattr(_m, "DB_PATH", None), id(getattr(_m, "get_conn", None)))
    except Exception:
        return None
    now = _time.time()
    if _names_cache["key"] == key and now - _names_cache["at"] < _NAMES_TTL_SECONDS:
        return _names_cache["rx"]
    rx = None
    try:
        conn = _m.get_conn()
        try:
            rows = conn.execute("SELECT name, location_name FROM restaurants").fetchall()
        finally:
            conn.close()
        names = [r["name"] for r in rows] + [r["location_name"] for r in rows]
        rx = _names_regex(names)
        try:
            _m.on_restaurant_change(invalidate_tenant_names)
        except Exception:
            pass
    except Exception as e:
        print(f"[privacy] tenant names unavailable: {e}")
    _names_cache.update({"key": key, "at": now, "rx": rx})
    return rx


def _value_problem(s, names_rx):
    if _TENANT_ID_RE.search(s):
        return "a restaurant id"
    if _EMAIL_IN_VALUE_RE.search(s):
        return "an email address"
    if names_rx is not None:
        m = names_rx.search(s)
        if m:
            return f"a restaurant's name ({m.group(0)!r})"
    return None


def assert_anonymous(payload, path="payload", deny_names=None, _rx=False):
    """Raise PrivacyError if any key in `payload` (recursively) identifies a
    restaurant, a person or an exact figure, or any string value carries a
    tenant's name, a restaurant id or an email address. `deny_names`
    replaces the tenant-name list (tests; a caller that already holds it).
    Returns the payload so it can be used inline:
    `return assert_anonymous(build())`."""
    if _rx is False:
        _rx = _names_regex(deny_names) if deny_names is not None else _tenant_names_regex()
    if isinstance(payload, dict):
        for k, v in payload.items():
            key = str(k).lower()
            if key not in ALLOWED_KEYS and any(stem in key for stem in FORBIDDEN_KEY_STEMS):
                raise PrivacyError(f"{path}.{k} must not leave the engine")
            assert_anonymous(v, f"{path}.{k}", _rx=_rx)
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            assert_anonymous(v, f"{path}[{i}]", _rx=_rx)
    elif isinstance(payload, str):
        why = _value_problem(payload, _rx)
        if why:
            raise PrivacyError(f"{path} carries {why} and must not leave the engine")
    return payload


def strip_identity(row: dict) -> dict:
    """The safe subset of a features row for cross-restaurant use: the
    features themselves and the cohort, never the restaurant id."""
    return {"cohort": row.get("cohort"), "week": row.get("week"),
            "features": dict(row.get("features") or {}), "completeness": row.get("completeness")}
