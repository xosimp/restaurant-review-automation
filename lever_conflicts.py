"""
lever_conflicts.py — advice from different modules that pulls against
itself, reconciled where cards are assembled, and the owner's choice
remembered (memory audit 9/29/26, "conflicts").

Advice signatures (insight_store.advice_signature) match the same lever on
the same subject; nothing matched two levers that fight. Home showed
"Reprice Short Rib to $34.25" while the dish scorecard said guests call it
poor value; the Marketing feed could promote a dish whose main ingredient
was critically low; "Fill Tuesday" and "Trim Tuesday" appeared together.

The map (RULES):

  trim_vs_fill      labor:day:X  against  guest_outreach:day:X or
                    marketing:day:X — two cards, the weaker annotated.
  reprice_vs_value  pricing:dish:X  against guests calling X poor value
                    (its reviews' complaints) — the card annotated, its
                    confidence lowered where it is built (menu_intelligence).
  promote_vs_stock  marketing:dish:X  against a critically low ingredient of
                    X (its recipe) — the card annotated.

`apply` annotates the weaker side with `conflict` {id, rule, with, why,
choose, route}; the owner's choice (POST /recs/conflict → record_choice) is
stored as a decision in rec_ledger (a bookkeeping key "conflict:<id>"), and
from then on the same conflict resolves the same way: the card the owner
did not keep is HELD (left out, returned in `held`) for CHOICE_DAYS.

Called by Home, the one-thing pick, the DSR's action ranking and the
Marketing feed. Deterministic; no model. Never raises into a caller.
"""
import re
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH

RULES = ("trim_vs_fill", "reprice_vs_value", "promote_vs_stock")
RULE_LABELS = {"trim_vs_fill": "Trimming a day you are also trying to fill",
               "reprice_vs_value": "Raising the price of a dish guests call poor value",
               "promote_vs_stock": "Promoting a dish whose ingredient is running out"}
# How long an owner's choice holds the same conflict's answer.
CHOICE_DAYS = 365
CHOICE_PREFIX = "conflict:"
# What a guest complaint about value reads like (review specific_complaint).
_VALUE_RE = re.compile(r"\b(?:price[ds]?|pricey|expensive|overpriced|over-priced|value|worth|cost(?:s|ly)?|"
                       r"small portions?|tiny portions?|portion size|rip[- ]?off)\b", re.I)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _sig(card):
    sig = card.get("advice_signature")
    if sig is None:
        try:
            import insight_store
            sig = insight_store.advice_signature(card.get("key"), card.get("title") or card.get("what")
                                                 or card.get("text"))
        except Exception:
            sig = None
    return sig


def _score(card):
    for f in ("rank_score", "score"):
        try:
            if card.get(f) is not None:
                return float(card[f])
        except (TypeError, ValueError):
            pass
    try:
        return float(card.get("dollars_monthly") or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split())


_FACTS_CACHE = {}
_FACTS_TTL = 60


def facts(restaurant_id, db_path=DB_PATH, critical_low=None) -> dict:
    """facts_uncached, with the read of the inventory analysis cached for
    _FACTS_TTL seconds when the caller brings no critical-low list (Home,
    the one-thing pick and the feed are built together)."""
    import time as _t
    if critical_low is not None:
        return facts_uncached(restaurant_id, db_path=db_path, critical_low=critical_low)
    k = (db_path, restaurant_id)
    hit = _FACTS_CACHE.get(k)
    if hit and _t.time() - hit[0] < _FACTS_TTL:
        return hit[1]
    out = facts_uncached(restaurant_id, db_path=db_path)
    if len(_FACTS_CACHE) > 500:
        _FACTS_CACHE.clear()
    _FACTS_CACHE[k] = (_t.time(), out)
    return out


def facts_uncached(restaurant_id, db_path=DB_PATH, critical_low=None) -> dict:
    """What the fact-side rules read, once per build: {"value": {dish:
    {"n", "sample"}}} — guest complaints about a dish's value in the
    scorecard's window — and {"low": {dish: [ingredients]}} — each dish's
    critically low ingredients (`critical_low`: the build's own inventory
    analysis list, else read). Dish names normalised. Never raises."""
    out = {"value": {}, "low": {}}
    try:
        import menu_intelligence as mi
        for m in mi._dish_mentions(restaurant_id, db_path=db_path):
            if m.get("sentiment") != "negative" or not _VALUE_RE.search(str(m.get("complaint") or "")):
                continue
            d = out["value"].setdefault(_norm(m["dish"]), {"n": 0, "sample": None})
            d["n"] += 1
            d["sample"] = d["sample"] or str(m.get("complaint") or "")[:120]
    except Exception as e:
        print(f"[lever_conflicts] value complaints unavailable for {restaurant_id}: {e}")
    try:
        if critical_low is None:
            from inventory import analysis_for
            items, is_live, analysis = analysis_for(restaurant_id)
            critical_low = (analysis or {}).get("critical_low") or [] if (items and is_live) else []
        low = {_norm(x.get("item")) for x in critical_low or [] if x.get("item")}
        if low:
            conn = get_conn(db_path)
            try:
                for r in conn.execute(
                        "SELECT m.name AS dish, g.name AS item FROM recipe_ingredients ri "
                        "JOIN menu_items m ON m.id=ri.menu_item_id JOIN ingredients g ON g.id=ri.ingredient_id "
                        "WHERE m.restaurant_id=? AND COALESCE(m.is_active,1)=1", (restaurant_id,)).fetchall():
                    if _norm(r["item"]) in low:
                        out["low"].setdefault(_norm(r["dish"]), []).append(str(r["item"]))
            finally:
                conn.close()
    except Exception as e:
        print(f"[lever_conflicts] stock conflicts unavailable for {restaurant_id}: {e}")
    return out


def _parts(sig):
    """("labor", "day", "tuesday") from "labor:day:tuesday", else None."""
    bits = str(sig or "").split(":", 2)
    return tuple(bits) if len(bits) == 3 else None


LIVE_DAYS = 7


def live_cards(restaurant_id, exclude=(), db_path=DB_PATH) -> list:
    """What another surface is saying right now: open episodes shown in the
    last LIVE_DAYS with a stored advice signature, as comparison-only cards
    ({key, title, advice_signature, other: True}) — "Fill Tuesday" on the
    Marketing feed is a conflict for Home's "Trim Tuesday". Never raises."""
    since = (datetime.utcnow() - timedelta(days=LIVE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT key, title, signature FROM rec_instances WHERE restaurant_id=? AND status='open' "
            "AND signature IS NOT NULL AND signature != '' AND last_event_at >= ? LIMIT 200",
            (restaurant_id, since)).fetchall()
    except Exception as e:
        print(f"[lever_conflicts] live cards unreadable for {restaurant_id}: {e}")
        return []
    finally:
        conn.close()
    skip = set(exclude or ())
    return [{"key": r["key"], "title": r["title"], "advice_signature": r["signature"], "other": True}
            for r in rows if r["key"] not in skip]


def find(cards, fx=None, others=()) -> list:
    """The conflicts among `cards` (each {key, title, advice_signature?,
    rank_score | score}), with `others` (what other surfaces are saying —
    live_cards; compared, never annotated) and against `fx` (facts()):
    [{id, rule, a, b, weaker, why, choose}] — `b` is None for a card
    against a fact; against another surface's card, this surface's is the
    one annotated."""
    fx = fx or {"value": {}, "low": {}}
    sigs = [(c, _sig(c)) for c in list(cards or []) + [dict(o, other=True) for o in others or ()]]
    out = []
    by_day = {}
    for c, s in sigs:
        p = _parts(s)
        if p and p[1] == "day":
            by_day.setdefault(p[2], []).append((c, p[0]))
    for day, group in by_day.items():
        trims = [c for c, fam in group if fam == "labor"]
        fills = [c for c, fam in group if fam in ("guest_outreach", "marketing")]
        for t in trims:
            for f in fills:
                if t.get("other") and f.get("other"):
                    continue                     # neither is this surface's to settle
                if t.get("other") or f.get("other"):
                    weaker = f if t.get("other") else t
                else:
                    weaker = t if _score(t) < _score(f) else f
                out.append({"id": f"trim_vs_fill:labor:day:{day}", "rule": "trim_vs_fill", "a": t["key"],
                            "b": f["key"], "weaker": weaker["key"],
                            "choose": [{"signature": f"labor:day:{day}", "key": t["key"],
                                        "label": t.get("title") or t["key"]},
                                       {"signature": _sig(f), "key": f["key"], "label": f.get("title") or f["key"]}],
                            "why": (f"{day.capitalize()}: one card trims staffing, another tries to bring guests "
                                    f"in — they pull against each other.")})
    for c, s in sigs:
        p = _parts(s)
        if not p or p[1] != "dish" or c.get("other"):
            continue
        dish = _norm(p[2])
        if p[0] == "pricing" and fx["value"].get(dish):
            v = fx["value"][dish]
            out.append({"id": f"reprice_vs_value:{s}", "rule": "reprice_vs_value", "a": c["key"], "b": None,
                        "weaker": c["key"], "choose": [{"signature": s, "key": c["key"], "label": "Keep it"},
                                                       {"signature": "hold", "key": None, "label": "Hold it"}],
                        "why": (f"Guests called it poor value in {v['n']} review{'s' if v['n'] != 1 else ''} "
                                f"recently — a higher price may cost more than it earns.")})
        if p[0] == "marketing" and fx["low"].get(dish):
            items = fx["low"][dish]
            out.append({"id": f"promote_vs_stock:{s}", "rule": "promote_vs_stock", "a": c["key"], "b": None,
                        "weaker": c["key"], "choose": [{"signature": s, "key": c["key"], "label": "Keep it"},
                                                       {"signature": "hold", "key": None, "label": "Hold it"}],
                        "why": (f"{', '.join(items[:2])} for this dish {'is' if len(items) == 1 else 'are'} "
                                f"critically low — promoting it now may sell what you can't make.")})
    return out


def choices(restaurant_id, db_path=DB_PATH, now=None) -> dict:
    """{conflict id: {"prefer", "on"}} — the owner's stored choices still
    holding (CHOICE_DAYS). Never raises."""
    import json
    now = now or datetime.utcnow()
    since = (now - timedelta(days=CHOICE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        for r in conn.execute("SELECT key, meta, at FROM rec_events WHERE restaurant_id=? AND event='accepted' "
                              "AND key LIKE ? AND at >= ? ORDER BY at, id",
                              (restaurant_id, CHOICE_PREFIX + "%", since)).fetchall():
            try:
                meta = json.loads(r["meta"] or "{}") or {}
            except (TypeError, ValueError):
                meta = {}
            if meta.get("prefer"):
                out[r["key"][len(CHOICE_PREFIX):]] = {"prefer": meta["prefer"], "on": r["at"]}
    except Exception as e:
        print(f"[lever_conflicts] choices unreadable for {restaurant_id}: {e}")
    finally:
        conn.close()
    return out


def record_choice(restaurant_id, conflict_id, prefer, user=None, db_path=DB_PATH) -> dict:
    """The owner's choice on a conflict, stored as a decision (a bookkeeping
    episode in rec_ledger, out of every acceptance figure) so the same
    conflict resolves the same way next time. `prefer` is the advice
    signature kept, or "hold". An admin's view-as choice is support's, not
    the owner's (it is kept, and applies to nothing)."""
    cid = str(conflict_id or "").strip()[:140]
    prefer = str(prefer or "").strip()[:120]
    if not cid or not prefer or not cid.split(":", 1)[0] in RULES:
        return {"ok": False, "error": "No such conflict."}
    import rec_ledger
    authority = None
    try:
        from permissions import answer_authority
        authority = answer_authority(user) if user else None
    except Exception:
        authority = None
    ok = rec_ledger.record(restaurant_id, CHOICE_PREFIX + cid, "accepted", surface="home",
                           user_id=(user or {}).get("id") if isinstance(user, dict) else None,
                           meta={"prefer": prefer, "module": "home"}, authority=authority,
                           via=rec_ledger.request_via(user) if isinstance(user, dict) else None, db_path=db_path)
    return {"ok": bool(ok), "conflict": cid, "prefer": prefer,
            "message": "Noted — Cavnar AI will settle this the same way next time"}


def apply(restaurant_id, cards, fx=None, db_path=DB_PATH, held_out=None, others=None) -> list:
    """`cards` with each conflict settled: a card the owner already chose
    against is HELD (removed; appended to `held_out` with its conflict when
    a list is given); otherwise the weaker card carries `conflict`
    {id, rule, label, with, why, choose, route} for the owner to settle.
    `others` — what other surfaces are saying (default live_cards).
    Returns the kept cards in their order. Never raises."""
    try:
        if others is None:
            others = live_cards(restaurant_id, exclude=[c.get("key") for c in cards or []], db_path=db_path)
        found = find(cards, fx, others=others)
    except Exception as e:
        print(f"[lever_conflicts] find failed for {restaurant_id}: {e}")
        return list(cards or [])
    if not found:
        return list(cards or [])
    try:
        chosen = choices(restaurant_id, db_path=db_path)
    except Exception:
        chosen = {}
    by_key = {c.get("key"): c for c in cards}
    held = set()
    for cf in found:
        pick = chosen.get(cf["id"])
        if pick:
            keep = pick["prefer"]
            if keep == "hold":
                drop = [cf["a"]]
            elif any(o["signature"] == keep for o in cf["choose"]):
                drop = [o["key"] for o in cf["choose"] if o["key"] and o["signature"] != keep]
            else:
                drop = []                  # a choice about other advice: ask again
            for k in drop:
                held.add(k)
                if held_out is not None:
                    held_out.append(dict(cf, held_key=k, resolved=pick))
            if drop or keep != "hold":
                continue
        card = by_key.get(cf["weaker"])
        if card is None or card.get("conflict"):
            continue
        other_key = (cf["b"] if cf["weaker"] == cf["a"] else cf["a"]) if cf.get("b") else None
        other = by_key.get(other_key) or next((o for o in others or () if o.get("key") == other_key), None)
        card["conflict"] = {"id": cf["id"], "rule": cf["rule"], "label": RULE_LABELS.get(cf["rule"]),
                            "with": (other or {}).get("title") if other else None, "why": cf["why"],
                            "choose": cf["choose"], "route": {"web": "/api/recs/conflict",
                                                              "mobile": "/mobile/api/recs/conflict"}}
    return [c for c in cards if c.get("key") not in held]
