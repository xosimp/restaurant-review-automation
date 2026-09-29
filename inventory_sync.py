"""
inventory_sync.py — one front door for a restaurant's inventory system.

Food Cost runs on tables it already owns: `ingredients` (the item master —
name, unit, unit cost, par, supplier) and `ingredient_stock_events` (counts,
deliveries, waste, depletion). Until an inventory system is connected the
owner fills them by hand: the count sheet, the price monitor, the supplier
dropdowns. Once one is connected and has synced, the same tables fill from
it every night (the 3am POS job runs `sync_all`) and the hand-entry surfaces
step aside — `status(rid)["synced"]` is what the pages read. Nothing
downstream changes: waste, days on hand, what to order, food cost % and the
AI reads all read the tables, not the source (owner, 9/26/26;
docs/plans/BACK_OFFICE_INTEGRATION.md).

A provider is a module named in PROVIDERS exposing:

    is_connected(restaurant_id) -> bool
    connected_ids() -> [restaurant_id, ...]     every restaurant it can sync
    label -> str                                 "Back Office"
    fetch_inventory(restaurant_id) -> {
        "items":   [{"name", "unit", "category", "unit_cost", "par_level",
                     "case_size", "supplier_name", "supplier_email", "ref"}],
        "counts":  [{"ref" or "name", "qty", "counted_on": "YYYY-MM-DD"}],
        "recipes": [{"dish", "ref", "sell_price",
                     "lines": [{"ref" or "name", "qty"}]}],   # qty per plate, in the item's unit
    }

A field a provider leaves out (None) keeps what the owner set; a count is a
recount event (the ledger's anchor) tagged with the provider's name, written
once per ingredient and day. A synced dish's recipe is the provider's: its
lines are replaced on every sync (menu_items + recipe_ingredients), which is
what plate cost, margins and depletion read. Invoice lines are the next
contract field; they are not read yet.

PROVIDERS is resolved by module name at call time (importlib) — a dynamic
reference: grep for "PROVIDERS" before renaming a provider module.
"""
import importlib
import logging
import math
import os
from datetime import date, datetime, timezone

log = logging.getLogger("inventory_sync")

PROVIDERS = {"backoffice": "backoffice"}
SYNC_CURSOR_KEY = "inventory_sync_cursor"
SYNC_MAX_SECONDS = int(os.getenv("INVENTORY_SYNC_MAX_SECONDS", str(15 * 60)))


def get_conn(*a, **k):
    import models
    return models.get_conn(*a, **k)


def _module(name):
    return importlib.import_module(PROVIDERS[name])


def connected_provider(restaurant_id):
    """(name, module) of the provider this restaurant syncs from, or (None, None)."""
    for name in PROVIDERS:
        try:
            mod = _module(name)
            if mod.is_connected(restaurant_id):
                return name, mod
        except Exception as e:
            log.warning(f"{name}.is_connected failed for {restaurant_id}: {e}")
    return None, None


def connected_ids():
    out = set()
    for name in PROVIDERS:
        try:
            out |= set(_module(name).connected_ids() or [])
        except Exception as e:
            log.warning(f"{name}.connected_ids failed: {e}")
    return sorted(out)


def status(restaurant_id, db_path=None) -> dict:
    """What the Food Cost page needs to know about the inventory source.
    `synced` is True only when a provider is connected AND its data landed
    (client_data.inventory_source is that provider with a sync time) — a
    key saved before the first successful sync leaves hand entry in place."""
    name, mod = connected_provider(restaurant_id)
    row = None
    try:
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            row = conn.execute("SELECT inventory_source, inventory_synced_at, inventory_sync_error "
                               "FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"status: client_data unreadable for {restaurant_id}: {e}")
    src = row["inventory_source"] if row else None
    at = row["inventory_synced_at"] if row else None
    synced = bool(name and src == name and at)
    return {"provider": name, "label": getattr(mod, "label", None) if mod else None,
            "connected": bool(name), "synced": synced, "synced_at": at if synced else None,
            "error": (row["inventory_sync_error"] if row else None) if name else None}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _day(v):
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _stamp(conn, restaurant_id, provider, error=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    exists = conn.execute("SELECT 1 FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    if error is None:
        if exists:
            conn.execute("UPDATE client_data SET inventory_source=?, inventory_synced_at=?, inventory_sync_error=NULL "
                         "WHERE restaurant_id=?", (provider, now, restaurant_id))
        else:
            conn.execute("INSERT INTO client_data (restaurant_id, inventory_source, inventory_synced_at) VALUES (?,?,?)",
                         (restaurant_id, provider, now))
    elif exists:
        conn.execute("UPDATE client_data SET inventory_sync_error=? WHERE restaurant_id=?",
                     (str(error)[:300], restaurant_id))
    else:
        conn.execute("INSERT INTO client_data (restaurant_id, inventory_sync_error) VALUES (?,?)",
                     (restaurant_id, str(error)[:300]))


_ITEM_FIELDS = ("unit", "category", "unit_cost", "par_level", "case_size", "supplier_name", "supplier_email")


def apply_inventory(restaurant_id, provider, payload, db_path=None) -> dict:
    """Write a provider's normalized inventory into Food Cost's own tables.
    Idempotent: items match by the provider's ref, else by name (case- and
    space-insensitive); a count already recorded for that ingredient, day and
    source is not written twice. Returns {"ok", "items", "created", "counts"}."""
    import inventory_ledger as _il
    payload = payload or {}
    conn = get_conn(db_path) if db_path else get_conn()
    touched, created, counted, dishes = set(), 0, 0, 0
    try:
        rows = conn.execute("SELECT id, name, external_ref FROM ingredients WHERE restaurant_id=? "
                            "AND COALESCE(is_active,1)=1", (restaurant_id,)).fetchall()
        by_ref = {str(r["external_ref"]): r["id"] for r in rows if r["external_ref"]}
        by_name = {" ".join(str(r["name"]).lower().split()): r["id"] for r in rows}

        def _find(ref, name):
            if ref and str(ref) in by_ref:
                return by_ref[str(ref)]
            key = " ".join(str(name or "").lower().split())
            return by_name.get(key) if key else None

        for it in payload.get("items") or []:
            if not isinstance(it, dict):
                continue
            name = " ".join(str(it.get("name") or "").split())[:120]
            if not name:
                continue
            vals = {}
            for f in _ITEM_FIELDS:
                v = it.get(f)
                if v is None:
                    continue
                if f in ("unit_cost", "par_level", "case_size"):
                    v = _num(v)
                    if v is None:
                        continue
                else:
                    v = str(v).strip()[:120]
                vals[f] = v
            ref = str(it.get("ref")).strip()[:80] if it.get("ref") not in (None, "") else None
            iid = _find(ref, name)
            if iid:
                sets = ", ".join(f"{k}=?" for k in vals) + (", " if vals else "") + "external_ref=COALESCE(?, external_ref)"
                args = [*vals.values(), ref]
                if "par_level" in vals:
                    sets += (", par_changed_at=CASE WHEN COALESCE(par_level, -1) <> COALESCE(?, -1) "
                             "THEN datetime('now') ELSE par_changed_at END")
                    args.append(vals["par_level"])
                conn.execute(f"UPDATE ingredients SET {sets} WHERE id=? AND restaurant_id=?",
                             (*args, iid, restaurant_id))
                if ref:
                    by_ref[ref] = iid
            else:
                cols = ["restaurant_id", "name", "external_ref"] + list(vals)
                cur = conn.execute(f"INSERT INTO ingredients ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                   (restaurant_id, name, ref, *vals.values()))
                iid = cur.lastrowid
                created += 1
                by_name[" ".join(name.lower().split())] = iid
                if ref:
                    by_ref[ref] = iid
            touched.add(iid)

        for c in payload.get("counts") or []:
            if not isinstance(c, dict):
                continue
            iid = _find(c.get("ref"), c.get("name"))
            qty, day = _num(c.get("qty")), _day(c.get("counted_on"))
            if not iid or qty is None or not day:
                continue
            if conn.execute("SELECT 1 FROM ingredient_stock_events WHERE restaurant_id=? AND ingredient_id=? "
                            "AND event_type='recount' AND event_date=? AND source=?",
                            (restaurant_id, iid, day, provider)).fetchone():
                continue
            conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                         " source, note) VALUES (?,?,?,?,?,?,?)",
                         (restaurant_id, iid, "recount", qty, day, provider, "synced count"))
            conn.execute("UPDATE ingredients SET last_recount_at=? WHERE id=? AND "
                         "(last_recount_at IS NULL OR last_recount_at<?)", (day, iid, day))
            touched.add(iid)
            counted += 1

        # Recipes: every dish the provider sends, its lines replaced by the
        # provider's (a line naming an ingredient the sync doesn't know is
        # left out, never guessed).
        mrows = conn.execute("SELECT id, name, external_ref FROM menu_items WHERE restaurant_id=? "
                             "AND COALESCE(is_active,1)=1", (restaurant_id,)).fetchall()
        m_ref = {str(r["external_ref"]): r["id"] for r in mrows if r["external_ref"]}
        m_name = {" ".join(str(r["name"]).lower().split()): r["id"] for r in mrows}
        for rc in payload.get("recipes") or []:
            if not isinstance(rc, dict):
                continue
            dish = " ".join(str(rc.get("dish") or rc.get("name") or "").split())[:120]
            if not dish:
                continue
            ref = str(rc.get("ref")).strip()[:80] if rc.get("ref") not in (None, "") else None
            price = _num(rc.get("sell_price"))
            mid = (m_ref.get(ref) if ref else None) or m_name.get(dish.lower())
            if mid:
                conn.execute("UPDATE menu_items SET sell_price=COALESCE(?, sell_price), external_ref=COALESCE(?, external_ref) "
                             "WHERE id=? AND restaurant_id=?", (price, ref, mid, restaurant_id))
            else:
                mid = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price, external_ref) "
                                   "VALUES (?,NULL,?,?,?)", (restaurant_id, dish, price, ref)).lastrowid
                m_name[dish.lower()] = mid
                if ref:
                    m_ref[ref] = mid
            lines = []
            for ln in rc.get("lines") or []:
                if not isinstance(ln, dict):
                    continue
                iid, q = _find(ln.get("ref"), ln.get("name")), _num(ln.get("qty"))
                if iid and q:
                    lines.append((iid, q))
            if not lines:
                continue
            conn.execute("DELETE FROM recipe_ingredients WHERE menu_item_id=?", (mid,))
            for iid, q in lines:
                conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?) "
                             "ON CONFLICT(menu_item_id, ingredient_id) DO UPDATE SET qty_per_unit=excluded.qty_per_unit",
                             (mid, iid, q))
            dishes += 1

        for iid in touched:
            _il.recompute_rollups(restaurant_id, iid, conn=conn)
        _stamp(conn, restaurant_id, provider)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "items": len(touched), "created": created, "counts": counted, "recipes": dishes}


def sync_restaurant(restaurant_id) -> dict:
    """Pull this restaurant's inventory from its connected system into the
    tables. A provider failure is stamped on client_data (the page says
    so) and never leaves partial writes: fetch first, then one transaction."""
    name, mod = connected_provider(restaurant_id)
    if not mod:
        return {"ok": False, "error": "No inventory system is connected."}
    try:
        payload = mod.fetch_inventory(restaurant_id)
    except Exception as e:
        log.warning(f"{name} fetch failed for {restaurant_id}: {e}")
        try:
            conn = get_conn()
            try:
                _stamp(conn, restaurant_id, name, error=e)
                conn.commit()
            finally:
                conn.close()
        except Exception as e2:
            import ops
            ops.capture(e2, job="inventory_sync", context=f"restaurant_id={restaurant_id} stamping a failure")
        return {"ok": False, "provider": name, "error": str(e)[:300]}
    out = apply_inventory(restaurant_id, name, payload)
    out["provider"] = name
    return out
