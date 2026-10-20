"""One external identity belongs to one restaurant (scale audit 10/19/26,
SEC-01).

Cavnar AI reads every client's Google Analytics and Search Console through
ONE shared Google service account. A client grants that account access to
their own property and pastes its id; the save used to check only that the
account COULD read what was pasted — never that the property was the
restaurant's. Any owner could paste a competitor's public domain
(`sc-domain:theirsite.com`) and, once that competitor had connected, the
nightly sync copied their searches into the wrong restaurant: a confused
deputy, worse with every tenant that connects.

This module is the ledger that closes it:

- `external_bindings` holds one row per (provider, external id, restaurant)
  with a status: `verified` (proved, or grandfathered at the fix) or
  `pending` (pasted, not proved yet). A source is READ only when its row is
  verified for that restaurant (`is_verified`), so nothing pasted reaches
  another tenant's data before proof.
- `conflict` refuses an id already VERIFIED for another live, non-demo
  restaurant (models.pos_binding_conflict's rule for POS stores). A pending
  row never blocks anyone — otherwise a squatter's paste would lock the
  real owner out.
- Proof itself is the provider's business (web_analytics.prove_ownership:
  the property's own website must be the restaurant's website, read from
  the admin-set Google listing or menu URL); an admin can approve a pending
  binding by hand (`approve`).
- `grandfather` records every binding that existed before the fix as
  verified, once (data_migrations), so today's clients keep reading.

Demo restaurants are exempt on both sides, as for POS stores and Place IDs.
"""
from __future__ import annotations

import logging

import models as _models

log = logging.getLogger(__name__)

# The providers this ledger holds and the restaurants column each mirrors.
PROVIDER_COLUMNS = {"ga4": "ga4_property_id", "gsc": "gsc_site_url"}
GRANDFATHER_MIGRATION = "external_bindings_grandfather_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_bindings (
    provider      TEXT    NOT NULL,          -- ga4 | gsc
    external_id   TEXT    NOT NULL,          -- normalised (normalize())
    restaurant_id INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',   -- verified | pending
    verified_how  TEXT,                      -- website_match | stream_match | admin | grandfathered | demo
    verified_at   TEXT,
    detail        TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, external_id, restaurant_id)
);
CREATE INDEX IF NOT EXISTS idx_external_bindings_rid ON external_bindings(restaurant_id, provider);
CREATE INDEX IF NOT EXISTS idx_external_bindings_status ON external_bindings(status, provider);
"""


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path is not None else _models.get_conn()


def init_external_bindings(db_path=None):
    """Boot-time schema and the one-time grandfathering (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    try:
        grandfather(db_path)
    except Exception as e:      # never stop a boot over the record
        log.warning("external_bindings grandfather skipped: %s", e)


def normalize(provider, raw) -> str:
    """The key one identity is held under. GA4: the property's digits.
    Search Console: the host, lower-case, without "www." — so
    "sc-domain:ejs.com", "https://www.ejs.com/" and "ejs.com" are ONE site
    and one restaurant can hold it. '' when it is not one."""
    import web_analytics as _wa
    if provider == "ga4":
        return _wa.clean_property_id(raw)
    if provider == "gsc":
        return site_host(_wa.clean_site_url(raw))
    return str(raw or "").strip().lower()


def site_host(site) -> str:
    """The host of a cleaned Search Console property ('' for none)."""
    from urllib.parse import urlparse
    text = str(site or "").strip().lower()
    if not text:
        return ""
    if text.startswith("sc-domain:"):
        host = text.split(":", 1)[1]
    else:
        host = urlparse(text).hostname or ""
    host = host.strip(".")
    return host[4:] if host.startswith("www.") else host


def _is_demo(restaurant_id, db_path=None) -> bool:
    try:
        r = _models.get_restaurant(restaurant_id, db_path) if db_path else _models.get_restaurant(restaurant_id)
    except Exception:
        return False
    if r is None:
        return False
    if int(getattr(r, "is_demo", 0) or 0):
        return True
    try:
        import demo_store
        return bool(demo_store.is_simulated(r))
    except Exception:
        return False


def exempt(restaurant_id, db_path=None) -> bool:
    """A demo or simulated restaurant: never bound, never gated."""
    return _is_demo(restaurant_id, db_path)


def conflict(provider, external_id, restaurant_id, db_path=None) -> bool:
    """True when `external_id` is VERIFIED for another live, non-demo
    restaurant that still holds it (a restaurant that disconnected, or was
    offboarded, no longer does). Blank never conflicts."""
    key = normalize(provider, external_id)
    if not key or provider not in PROVIDER_COLUMNS:
        return False
    col = PROVIDER_COLUMNS[provider]
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT b.restaurant_id, r.{col} AS current_value FROM external_bindings b "
            f"JOIN restaurants r ON r.id=b.restaurant_id "
            f"WHERE b.provider=? AND b.external_id=? AND b.status='verified' AND b.restaurant_id<>? "
            f"AND COALESCE(r.is_demo,0)=0", (provider, key, int(restaurant_id))).fetchall()
    finally:
        conn.close()
    return any(normalize(provider, r["current_value"]) == key for r in rows)


def binding(provider, restaurant_id, db_path=None):
    """This restaurant's row for `provider` (a dict) or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM external_bindings WHERE provider=? AND restaurant_id=? "
                           "ORDER BY created_at DESC LIMIT 1", (provider, int(restaurant_id))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def is_verified(provider, external_id, restaurant_id, db_path=None) -> bool:
    """Whether this restaurant may READ `external_id`: its own row is
    verified (and nobody else verified it first), or it is a demo."""
    if exempt(restaurant_id, db_path):
        return True
    key = normalize(provider, external_id)
    if not key:
        return False
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT status FROM external_bindings WHERE provider=? AND external_id=? "
                           "AND restaurant_id=?", (provider, key, int(restaurant_id))).fetchone()
    finally:
        conn.close()
    return bool(row) and row["status"] == "verified"


def bind(provider, external_id, restaurant_id, status, how=None, detail=None, db_path=None):
    """Record `restaurant_id`'s binding to `external_id` (replacing any
    other id it held for this provider). A verified write re-checks the
    conflict rule inside the same call; returns the status written, or
    'conflict' when another restaurant verified it first."""
    key = normalize(provider, external_id)
    if not key:
        unbind(provider, restaurant_id, db_path=db_path)
        return None
    if status == "verified" and conflict(provider, key, restaurant_id, db_path=db_path):
        return "conflict"
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM external_bindings WHERE provider=? AND restaurant_id=? AND external_id<>?",
                     (provider, int(restaurant_id), key))
        existing = conn.execute("SELECT status FROM external_bindings WHERE provider=? AND external_id=? "
                                "AND restaurant_id=?", (provider, key, int(restaurant_id))).fetchone()
        if existing and existing["status"] == "verified" and status != "verified":
            conn.commit()                 # a re-save of a proved id never demotes it
            return "verified"
        if existing:
            conn.execute("UPDATE external_bindings SET status=?, verified_how=?, verified_at=CASE WHEN ?='verified' "
                         "THEN datetime('now') ELSE NULL END, detail=? WHERE provider=? AND external_id=? "
                         "AND restaurant_id=?", (status, how, status, detail, provider, key, int(restaurant_id)))
        else:
            conn.execute("INSERT INTO external_bindings (provider, external_id, restaurant_id, status, verified_how, "
                         "verified_at, detail) VALUES (?,?,?,?,?,CASE WHEN ?='verified' THEN datetime('now') "
                         "ELSE NULL END,?)", (provider, key, int(restaurant_id), status, how, status, detail))
        conn.commit()
    finally:
        conn.close()
    return status


def unbind(provider, restaurant_id, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        n = conn.execute("DELETE FROM external_bindings WHERE provider=? AND restaurant_id=?",
                         (provider, int(restaurant_id))).rowcount
        conn.commit()
    finally:
        conn.close()
    return n or 0


def pending(db_path=None) -> list:
    """Every binding waiting for proof, oldest first (the admin's list)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT b.*, r.name AS restaurant_name FROM external_bindings b "
                            "LEFT JOIN restaurants r ON r.id=b.restaurant_id WHERE b.status='pending' "
                            "ORDER BY b.created_at").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def approve(provider, external_id, restaurant_id, db_path=None) -> str:
    """An admin's approval of a pending binding: 'verified', 'conflict'
    (another restaurant holds it — move it there first) or 'missing'."""
    key = normalize(provider, external_id)
    b = binding(provider, restaurant_id, db_path=db_path)
    if not b or b["external_id"] != key:
        return "missing"
    return bind(provider, key, restaurant_id, "verified", how="admin", detail="approved by an admin",
                db_path=db_path)


def grandfather(db_path=None) -> int:
    """Once (data_migrations): every GA4 property and Search Console site
    bound before 10/19/26 is recorded as verified ('grandfathered'), so a
    client already reading keeps reading. Duplicates already in place are
    recorded too (each restaurant's own row) and named in the migration's
    detail for a person to resolve — the fix refuses new duplicates, it
    does not silently cut a live client off. Returns rows recorded."""
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (GRANDFATHER_MIGRATION,)).fetchone():
            conn.rollback()
            return 0
        rows = conn.execute("SELECT id, ga4_property_id, gsc_site_url, COALESCE(is_demo,0) AS is_demo "
                            "FROM restaurants").fetchall()
        recs, seen, dups = [], {}, []
        for r in rows:
            if int(r["is_demo"] or 0):
                continue
            for provider, col in PROVIDER_COLUMNS.items():
                key = normalize(provider, r[col])
                if not key:
                    continue
                if (provider, key) in seen:
                    dups.append(f"{provider}:{key} on #{seen[(provider, key)]} and #{r['id']}")
                seen.setdefault((provider, key), r["id"])
                recs.append((provider, key, int(r["id"])))
        if recs:
            conn.executemany("INSERT OR IGNORE INTO external_bindings (provider, external_id, restaurant_id, status, "
                             "verified_how, verified_at, detail) VALUES (?,?,?,'verified','grandfathered',"
                             "datetime('now'),'bound before the ownership check (10/19/26)')", recs)
        detail = f"{len(recs)} bindings grandfathered" + (f"; duplicates to resolve: {', '.join(dups)}" if dups else "")
        conn.execute("INSERT INTO data_migrations (name, detail) VALUES (?, ?)", (GRANDFATHER_MIGRATION, detail))
        conn.commit()
        if dups:
            log.warning("external_bindings: %s", detail)
        return len(recs)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
