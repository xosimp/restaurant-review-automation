"""
bar/cache.py — the Bar page's payload, kept per restaurant until the data
under it changes (bar fix round 10/15/26, V17 / SEC-2 / RPW-18).

Every open of the Bar used to rebuild the payload from every invoice line
ever, and sorting 30 products cost 30 full builds. Now the page, its phone
twin and its paging routes read `page(...)`:

  1. `stamp(rid, today)` — one cheap read of what the payload is built
     from: the restaurant's invoices (count, newest id, newest update, the
     sums of their totals and balanced flags — a re-read inside the same
     second still moves them), its Fintech files (purges too), the owner's
     product sorts, counts, bar mapping, stopped distributors and W5's
     settings / packs / links / deal sheet / drink map, W6b's drink specs
     and waste log, the nightly report's sales metrics, the archived bar
     nights and the ticket archive's days, the POS catalog and menu prices, the restaurant's own row (targets, hours, fiscal
     calendar, timezone), the Bar's shared write generation and the day.
  2. The payload is kept per (database, restaurant) with the stamp it was
     built on; a request whose stamp matches is served the kept payload.
     A burst of opens for one restaurant shares one build (single-flight).
  3. Every Bar write route calls `invalidate(rid)`: it drops this
     process's copy and moves the shared generation (a `job_cursors` row,
     `bar_gen:<rid>`), so every other process's stamp changes too — two
     sorts inside one second can't hide behind an unchanged timestamp.

Why a stamp and not a payload stored after intake and DSR writes: the
payload depends on ~15 tables written by six modules (intake, the DSR, the
POS archive, the owner's routes); a stamp read at request time cannot miss
a writer, where a materialise-on-write hook in each one can. A cache keyed
on a database stamp is right in every worker process (CLAUDE.md, gunicorn
workers), and the kept copy is bounded (MAX_ENTRIES, LRU) and never older
than TTL_SECONDS whatever the stamp says (the safety net for a writer this
list doesn't name yet).

The page payload is the full build with its long lists capped (PAGE_CAPS);
the whole lists are served a page at a time by `price_rows` and
`product_rows` (GET /api/food-cost/bar/prices and /products).
"""
import hashlib
import os
import threading
import time
from collections import OrderedDict
from datetime import date

import models as _models
from bar.common import get_conn

TTL_SECONDS = float(os.environ.get("BAR_CACHE_TTL_SECONDS", "600"))
MAX_ENTRIES = int(os.environ.get("BAR_CACHE_ENTRIES", "16"))
GEN_PREFIX = "bar_gen:"

# The price watch's lists and the products, capped in the page payload
# (each list's full length is in `totals`); the routes page through them.
PRICE_LISTS = ("increases", "decreases", "events", "unconfirmed", "deals", "tiers", "minor", "credits",
               "ladders", "tier_flags", "split_case")
PRICE_CAP = 50
PRODUCT_CAP = 100
PAGE_LIMIT_MAX = 200

# (table, aggregates) read per restaurant for the stamp; each is one
# indexed read on restaurant_id. A table missing (an older database) reads
# as absent.
_SOURCES = (
    ("fintech_invoices", "COUNT(*), MAX(id), MAX(updated_at), SUM(COALESCE(lines_total, 0)), "
                         "SUM(COALESCE(total, 0)), SUM(COALESCE(balanced, 0))"),
    ("fintech_files", "COUNT(*), MAX(id), SUM(CASE WHEN status='purged' THEN 1 ELSE 0 END)"),
    ("bar_product_categories", "COUNT(*), MAX(set_at)"),
    ("bar_counts", "COUNT(*), MAX(entered_at), SUM(value_usd)"),
    ("bar_sales_map", "COUNT(*), MAX(updated_at), MAX(confirmed_at)"),
    ("bar_sales_nights", "COUNT(*), MAX(computed_at)"),
    ("bar_stopped_distributors", "COUNT(*), MAX(set_at)"),
    ("bar_settings", "COUNT(*), MAX(set_at)"),
    ("bar_product_packs", "COUNT(*), MAX(set_at)"),
    ("bar_product_links", "COUNT(*), MAX(set_at)"),
    ("bar_expected_prices", "COUNT(*), MAX(id), MAX(set_at)"),
    ("bar_drink_map", "COUNT(*), MAX(set_at)"),
    ("bar_drink_specs", "COUNT(*), MAX(set_at), SUM(oz)"),
    ("bar_waste", "COUNT(*), MAX(id), MAX(entered_at)"),
    # Back Office's entries and how they sort (backoffice_ap, 10/15/26).
    ("bo_invoices", "COUNT(*), MAX(updated_at), SUM(amount)"),
    ("bo_gl_accounts", "COUNT(*), MAX(updated_at)"),
    ("bo_vendor_categories", "COUNT(*), MAX(set_at)"),
    # The ticket archive (variance reads ~8 weeks of pos_ticket_lines): a
    # night archived or re-archived writes its pos_archive_days row, so the
    # day rows stand for the lines without scanning them.
    ("pos_archive_days", "COUNT(*), MAX(archived_at), SUM(lines), SUM(restated)"),
    ("pos_menu_prices", "COUNT(*), MAX(seen_at)"),
    ("pos_sales_categories", "COUNT(*), MAX(seen_at)"),
    ("pos_menu_catalog", "COUNT(*), MAX(seen_at)"),
)
# The nightly report's sales metrics, as a range on the (restaurant, metric,
# date) index (never LIKE).
_DSR_SQL = ("SELECT COUNT(*), MAX(updated_at) FROM dsr_metrics WHERE restaurant_id=? "
            "AND metric >= 'sales.' AND metric < 'sales/'")

_lock = threading.Lock()
_kept = OrderedDict()            # key -> {"stamp", "payload", "at"}
_flights = {}                    # key -> threading.Lock (one build per key at a time)
_local_gen = {}                  # key -> int: this process's own invalidations


def _db_key(db_path):
    """Which database a kept payload belongs to: a test that points get_conn
    at its own file is never served another's payload."""
    return (str(db_path) if db_path else str(getattr(_models, "DB_PATH", "")),
            id(getattr(_models, "get_conn", None)))


def _key(restaurant_id, db_path):
    return _db_key(db_path) + (int(restaurant_id),)


def _read(conn, sql, args):
    try:
        row = conn.execute(sql, args).fetchone()
    except Exception:
        try:
            conn.rollback()      # Postgres: a failed statement aborts the transaction
        except Exception:
            pass
        return None
    return tuple(row) if row is not None else None


def stamp(restaurant_id, db_path=None, today=None) -> str:
    """A digest of everything the Bar payload is built from (module note)."""
    rid = int(restaurant_id)
    parts = [str(today or ""), str(_local_gen.get(_key(rid, db_path), 0))]
    conn = get_conn(db_path)
    try:
        for table, aggs in _SOURCES:
            parts.append(repr(_read(conn, f"SELECT {aggs} FROM {table} WHERE restaurant_id=?", (rid,))))
        parts.append(repr(_read(conn, _DSR_SQL, (rid,))))
        parts.append(repr(_read(conn, "SELECT * FROM restaurants WHERE id=?", (rid,))))
        parts.append(repr(_read(conn, "SELECT value FROM job_cursors WHERE key=?", (GEN_PREFIX + str(rid),))))
    finally:
        conn.close()
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def invalidate(restaurant_id, db_path=None) -> None:
    """A Bar write happened: drop this process's copy and move the shared
    generation so every process rebuilds. Never raises."""
    rid = int(restaurant_id)
    key = _key(rid, db_path)
    with _lock:
        _kept.pop(key, None)
        _local_gen[key] = _local_gen.get(key, 0) + 1
    try:
        import uuid
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (GEN_PREFIX + str(rid), uuid.uuid4().hex))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[bar.cache] shared generation not moved for {rid}: {e}")


def clear() -> None:
    """Drop every kept payload in this process (tests)."""
    with _lock:
        _kept.clear()
        _flights.clear()


def full(restaurant_id, db_path=None, today=None, build=None) -> dict:
    """The whole payload (bar.build.build), served from the kept copy while
    its stamp holds. `build` is the builder (tests count its calls)."""
    if build is None:
        from bar.build import build as _build
        build = _build
    today = today or date.today()
    key = _key(restaurant_id, db_path)
    st = stamp(restaurant_id, db_path=db_path, today=today)
    hit = _hit(key, st)
    if hit is not None:
        return hit
    with _lock:
        flight = _flights.setdefault(key, threading.Lock())
    with flight:
        hit = _hit(key, st)          # another request built it while this one waited
        if hit is not None:
            return hit
        payload = build(restaurant_id, db_path=db_path, today=today)
        with _lock:
            _kept[key] = {"stamp": st, "payload": payload, "at": time.monotonic()}
            _kept.move_to_end(key)
            while len(_kept) > MAX_ENTRIES:
                _kept.popitem(last=False)
        return payload


def _hit(key, st):
    with _lock:
        e = _kept.get(key)
        if e and e["stamp"] == st and time.monotonic() - e["at"] <= TTL_SECONDS:
            _kept.move_to_end(key)
            return e["payload"]
    return None


def page(restaurant_id, db_path=None, today=None, build=None) -> dict:
    """The Bar page's payload: the full one with its long lists capped
    (the first PRICE_CAP of each price list, the first PRODUCT_CAP products,
    in the build's own order) and each list's full length in `totals`."""
    return trim(full(restaurant_id, db_path=db_path, today=today, build=build))


def trim(payload) -> dict:
    """A shallow copy of `payload` with the capped lists (module note)."""
    if not payload or not payload.get("available"):
        return payload
    out = dict(payload)
    pw = payload.get("price_watch")
    if isinstance(pw, dict):
        pw2 = dict(pw)
        totals = {}
        for name in PRICE_LISTS:
            lst = pw.get(name)
            if isinstance(lst, list):
                totals[name] = len(lst)
                pw2[name] = lst[:PRICE_CAP]
        pw2["totals"] = totals
        pw2["cap"] = PRICE_CAP
        pw2["truncated"] = any(n > PRICE_CAP for n in totals.values())
        out["price_watch"] = pw2
    prods = payload.get("products")
    if isinstance(prods, list):
        out["products"] = prods[:PRODUCT_CAP]
        out["products_total"] = len(prods)
        out["products_cap"] = PRODUCT_CAP
    return out


# ── the paging routes' reads ────────────────────────────────────────────────

def _bounds(offset, limit, default=50):
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise ValueError("Offset is a whole number.")
    try:
        limit = int(limit) if limit not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError("Limit is a whole number.")
    return offset, max(1, min(limit, PAGE_LIMIT_MAX))


def _text(v, cap=120):
    return " ".join(str(v or "").split()).lower()[:cap]


PRICE_SORTS = ("money", "pct", "newest")
PRICE_DIRS = ("up", "down", "all")


# The rows each direction reads when no list is named — the page's own
# segments: up = the increases that held, down = the decreases, all = those
# plus the unconfirmed changes, deals and order-size tiers (never `minor`).
DIR_LISTS = {"up": ("increases",), "down": ("decreases",),
             "all": ("increases", "decreases", "unconfirmed", "deals", "tiers")}


def price_rows(payload, offset=0, limit=50, q=None, dir="all", kind=None, cat=None, dist=None, sort="money",
               list_name=None) -> dict:
    """{ok, total, offset, limit, rows} — the price watch's rows, filtered
    and sorted: dir up|down|all picks the rows as the page does (DIR_LISTS)
    unless list_name names one of PRICE_LISTS (events: every row, then dir
    filters by the change's sign); kind (increase|decrease|deal|deal_ended|
    tier, several comma-separated: deal_ended,deal,tier); q (product name,
    description, distributor or key contains); cat (category key); dist
    (distributor contains); sort money (forward monthly dollars, largest
    move first) | pct | newest. Raises ValueError with an owner sentence on
    a bad parameter."""
    offset, limit = _bounds(offset, limit)
    dir = (dir or "all").lower()
    sort = (sort or "money").lower()
    if dir not in PRICE_DIRS:
        raise ValueError("Direction is up, down or all.")
    if sort not in PRICE_SORTS:
        raise ValueError("Sort by money, pct or newest.")
    if list_name not in (None, "") and list_name not in PRICE_LISTS:
        raise ValueError("That isn't one of the price watch's lists.")
    pw = (payload or {}).get("price_watch") or {}
    if list_name:
        rows = pw.get(list_name) or []
    else:
        rows, seen = [], set()
        for name in DIR_LISTS[dir]:
            for r in pw.get(name) or []:
                k = (r.get("product_key"), r.get("uom"), r.get("kind"), r.get("since")) if isinstance(r, dict) else id(r)
                if k not in seen:
                    seen.add(k)
                    rows.append(r)
    qq, dd, cc = _text(q), _text(dist), _text(cat, 40)
    kinds = {k.strip() for k in _text(kind, 200).split(",") if k.strip()}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ch = r.get("change")
        if dir == "up" and not (ch or 0) > 0:
            continue
        if dir == "down" and not (ch or 0) < 0:
            continue
        if kinds and str(r.get("kind") or "").lower() not in kinds:
            continue
        if cc and str(r.get("category") or "").lower() != cc:
            continue
        if dd and dd not in _text(r.get("distributor"), 200):
            continue
        if qq and not any(qq in _text(r.get(f), 300) for f in ("name", "product", "distributor", "product_key")):
            continue
        out.append(r)
    if sort == "money":
        out.sort(key=lambda r: -abs(r.get("forward_monthly_usd") or r.get("impact_90d") or 0))
    elif sort == "pct":
        out.sort(key=lambda r: -abs(r.get("pct") or 0))
    else:
        out.sort(key=lambda r: str(r.get("since") or r.get("date") or ""), reverse=True)
    return {"ok": True, "total": len(out), "offset": offset, "limit": limit, "rows": out[offset:offset + limit]}


def product_rows(payload, offset=0, limit=50, q=None, cat=None) -> dict:
    """{ok, total, offset, limit, rows} — the products in the page's order
    (unsorted first, then guesses, by spend), filtered by q (name,
    distributor or key contains) and cat (category key)."""
    offset, limit = _bounds(offset, limit)
    rows = (payload or {}).get("products") or []
    qq, cc = _text(q), _text(cat, 40)
    out = [r for r in rows if isinstance(r, dict)
           and (not cc or str(r.get("category") or "").lower() == cc)
           and (not qq or any(qq in _text(r.get(f), 300) for f in ("product", "distributor", "product_key")))]
    return {"ok": True, "total": len(out), "offset": offset, "limit": limit, "rows": out[offset:offset + limit]}


def expected_prices(restaurant_id, payload=None, db_path=None) -> list:
    """The owner's deal sheet for the page: [{id, product_key, product,
    min_qty, price, valid_from, valid_to, valid_from_mdy, valid_to_mdy,
    note}] — `product` the product's readable name from the payload's
    products when it is there, else its key."""
    from bar.common import _mdy, display_name
    from bar.products import deal_sheet
    names = {p.get("product_key"): p.get("product") for p in ((payload or {}).get("products") or [])
             if isinstance(p, dict)}
    out = []
    for r in deal_sheet(restaurant_id, db_path):
        k = r["product_key"]
        out.append({"id": r["id"], "product_key": k, "product": display_name(names.get(k)) if names.get(k) else k,
                    "min_qty": r["min_qty"], "price": r["price"], "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"], "valid_from_mdy": _mdy(r["valid_from"]) if r["valid_from"] else None,
                    "valid_to_mdy": _mdy(r["valid_to"]) if r["valid_to"] else None, "note": r["note"]})
    return out
