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
    "n", "n_with", "n_without", "restaurants", "cohort", "cohorts", "ownership",
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


# ── organisations, not locations (Benchmarking audit #9, BM1-5, BM2-4, BM4-2) ──
# A group owner with six of the eight "other" pizza locations reads a band
# that is 75% their own, and can solve the two real peers' figures out of
# it. Every floor counts ORGANISATIONS, the band a viewer sees leaves their
# whole organisation out, and no one organisation may be over a third of it.
MIN_ORGS = 5                  # distinct organisations behind a published band
MAX_ORG_SHARE = 1.0 / 3.0     # the largest organisation's share of a band


def _field(row, k):
    if isinstance(row, dict):
        return row.get(k)
    try:
        return row[k]
    except Exception:
        return getattr(row, k, None)


def _email(v) -> str:
    return str(v or "").strip().lower()


def org_key(row) -> str:
    """The organisation a restaurant belongs to, from its own row: its
    organization_id, else its (location group, owner email) pair, else its
    normalised owner email alone, else the restaurant (fix round R1-01 /
    R2-11: two ungrouped restaurants under one owner email were two
    "organisations", so one owner could clear the floors with their own
    sites and solve a real peer's figure out of the band). `row` is a dict,
    sqlite Row or object with those attributes.

    One row cannot see a login shared across restaurants or a Stripe
    customer; org_map() joins those as well, and the band builders read it."""
    org = _field(row, "organization_id")
    if org:
        return f"o{int(org)}"
    email = _email(_field(row, "owner_email"))
    group = (_field(row, "location_group") or "").strip().lower()
    if group:
        return f"g{group}|{email}"
    if email:
        return f"e{email}"
    return f"r{_field(row, 'id')}"


def legacy_org_key(row) -> str:
    """org_key as it was before the owner-email fallback — the key a band
    frozen before the fix round stored its members under. Read only to take
    a viewer's organisation out of such a band (benchmarks.viewer_org)."""
    org = _field(row, "organization_id")
    if org:
        return f"o{int(org)}"
    group = (_field(row, "location_group") or "").strip().lower()
    if group:
        return f"g{group}|{_email(_field(row, 'owner_email'))}"
    return f"r{_field(row, 'id')}"


# Logins whose access to two restaurants makes them one owner: owner-level
# roles only (a manager or employee who works two jobs does not merge two
# independent restaurants), never a Cavnar admin.
_OWNER_ROLES = ("owner", "client")
_ORG_COLS = "id, organization_id, location_group, owner_email, stripe_customer_id"


def _union_rows(rows, links) -> dict:
    """{restaurant_id: canonical key} — a union-find over `rows` joined by
    organisation, owner email and Stripe customer, plus the (restaurant,
    restaurant) pairs in `links`. A component's key is the smallest org_key
    in it, so it is stable while the component is."""
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by, keys = {}, {}
    for r in rows:
        rid = int(_field(r, "id"))
        parent[rid] = rid
        keys[rid] = org_key(r)
        for tag, v in (("o", _field(r, "organization_id")), ("e", _email(_field(r, "owner_email"))),
                       ("s", str(_field(r, "stripe_customer_id") or "").strip())):
            if v:
                by.setdefault((tag, str(v)), []).append(rid)
    for ids in by.values():
        for other in ids[1:]:
            union(ids[0], other)
    for a, b in links or ():
        if a in parent and b in parent:
            union(a, b)
    comp = {}
    for rid in parent:
        comp.setdefault(find(rid), []).append(rid)
    out = {}
    for ids in comp.values():
        canon = min(keys[i] for i in ids)
        for i in ids:
            out[i] = canon
    return out


def _login_links(conn, ids=None) -> list:
    """(restaurant, restaurant) pairs one owner-level login holds (its
    memberships and its home restaurant). No auth tables → no links."""
    marks = ",".join("?" * len(_OWNER_ROLES))
    try:
        rows = conn.execute(
            "SELECT m.user_id AS u, m.restaurant_id AS r FROM memberships m JOIN users us ON us.id=m.user_id "
            f"WHERE COALESCE(m.is_active,1)=1 AND COALESCE(us.is_admin,0)=0 AND LOWER(COALESCE(m.role,'')) IN ({marks}) "
            "UNION SELECT id AS u, restaurant_id AS r FROM users WHERE COALESCE(is_admin,0)=0 AND "
            f"LOWER(COALESCE(role,'client')) IN ({marks})", _OWNER_ROLES + _OWNER_ROLES).fetchall()
    except Exception:
        return []
    per_user = {}
    for r in rows:
        if r["r"] is not None:
            per_user.setdefault(r["u"], set()).add(int(r["r"]))
    links = []
    for rs in per_user.values():
        rs = sorted(rs)
        if len(rs) < 2 or (ids is not None and not (set(rs) & set(ids))):
            continue
        links.extend((rs[0], x) for x in rs[1:])
    return links


def _conn(db_path):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models as _m
    if db_path is None or db_path == getattr(_m, "DB_PATH", None):
        return _m.get_conn()
    return _m.get_conn(db_path)


def org_map(db_path=None) -> dict:
    """{restaurant_id: organisation key} for every restaurant: organisation,
    owner email, an owner-level login shared between restaurants and the
    Stripe customer, joined transitively (R1-01). What the nightly band
    builders count organisations by (jobs.member_info). Read lazily from the
    data layer, like the tenant names — this module is L0 otherwise.
    Consumers outside the band builders call org_key() and must not depend
    on this."""
    conn = _conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT {_ORG_COLS} FROM restaurants").fetchall()]
        links = _login_links(conn)
    finally:
        conn.close()
    return _union_rows(rows, links)


ORG_MAX_HOPS = 8     # an organisation is a handful of joins wide


def org_members(restaurant_id, db_path=None) -> tuple:
    """(organisation key, [row of every restaurant in it]) — the component
    org_map() puts `restaurant_id` in, found by widening from that one
    restaurant rather than scanning every row (a card asks per request)."""
    rid = int(restaurant_id)
    conn = _conn(db_path)
    have = {}
    try:
        frontier = {rid}
        for _ in range(ORG_MAX_HOPS):
            if not frontier:
                break
            q = ",".join("?" * len(frontier))
            for r in conn.execute(f"SELECT {_ORG_COLS} FROM restaurants WHERE id IN ({q})", tuple(frontier)):
                have[int(r["id"])] = dict(r)
            rows = [have[i] for i in frontier if i in have]
            found = set()
            for col, vals in (("organization_id", {r["organization_id"] for r in rows if r.get("organization_id")}),
                              ("LOWER(TRIM(owner_email))", {_email(r.get("owner_email")) for r in rows} - {""}),
                              ("TRIM(stripe_customer_id)", {str(r.get("stripe_customer_id") or "").strip()
                                                            for r in rows} - {""})):
                if vals:
                    vals = sorted(vals, key=str)
                    found |= {int(x[0]) for x in conn.execute(
                        f"SELECT id FROM restaurants WHERE {col} IN ({','.join('?' * len(vals))})",
                        tuple(vals)).fetchall()}
            for a, b in _login_links(conn, ids=set(have)):
                found |= {a, b}
            frontier = found - set(have)
        links = _login_links(conn, ids=set(have))
    finally:
        conn.close()
    if rid not in have:
        return f"r{rid}", []
    rows = list(have.values())
    return _union_rows(rows, links).get(rid) or f"r{rid}", rows


def org_hash(key) -> str:
    """A one-way stand-in for an organisation key, stored beside each member
    value server-side (never selected into a payload): enough to take a
    viewer's organisation out of a band and to count organisations."""
    import hashlib
    return hashlib.sha256(("cavnar-org:" + str(key)).encode()).hexdigest()[:16]


def org_counts(orgs) -> tuple[int, float]:
    """(distinct organisations, the largest one's share) over a member list
    of org hashes."""
    orgs = list(orgs or ())
    if not orgs:
        return 0, 0.0
    counts = {}
    for o in orgs:
        counts[o] = counts.get(o, 0) + 1
    return len(counts), max(counts.values()) / float(len(orgs))


def orgs_ok(n_orgs, max_share) -> bool:
    try:
        return int(n_orgs or 0) >= MIN_ORGS and float(max_share or 0) <= MAX_ORG_SHARE + 1e-9
    except (TypeError, ValueError):
        return False


def strip_identity(row: dict) -> dict:
    """The safe subset of a features row for cross-restaurant use: the
    features themselves and the cohort, never the restaurant id."""
    return {"cohort": row.get("cohort"), "week": row.get("week"),
            "features": dict(row.get("features") or {}), "completeness": row.get("completeness")}
