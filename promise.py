"""promise.py — what the sales audit estimated, against what actually happened.

The ROI audit (Sep 2026) found the sharpest gap in the product: Cavnar AI
could prove ROI to a PROSPECT and not to a CLIENT.

sales_audit_engine.py is the most rigorous financial model in this codebase.
It cites the National Restaurant Association's Operations Data Abstract, it
returns low/likely/high ranges rather than one falsely precise figure, it
refuses to double-count (overtime sits inside labor dollars, waste inside
food cost, bar variance inside pour cost), it states its recovery shares,
and a category with missing inputs comes back "insufficient" rather than
guessed. Will has to defend every number of it across a table from an owner.

Then the contract is signed and it never runs again. `sales_audits` even
carried a `linked_restaurant_id` column — declared, and read or written
nowhere in the codebase. The bridge was built and never connected.

This connects it. For every audited category that maps to a module and a
measurable metric, it puts three things side by side:

    what the audit ESTIMATED was available, as the range it actually gave
    where the metric stood AT THE AUDIT
    where it stands NOW

and nothing else. It does not claim the product caused the movement — the
same CAUSATION_CAVEAT that governs every outcome governs this, and more so
over a year. It does not score itself. An owner reading "we estimated
$18k–$34k a year from labor; you were at 34.2% then and 31.1% now" can do
the arithmetic that matters to them, which is the only honest version of
this comparison.

A category the audit could not size, or whose metric cannot be measured on
either side, is reported as exactly that. Never as zero, and never dropped
— a promise that turned out unmeasurable is itself worth knowing.
"""
import logging
from datetime import date, timedelta

import metrics

log = logging.getLogger(__name__)

# Audit category -> the metric that would show it moving. Only categories
# with an honest metric appear; `bar` and `waitlist` are sized by the audit
# but the product has no pour-cost or turn-time data, so they are carried
# as "not measurable here" rather than silently dropped.
CATEGORY_METRIC = {
    "labor": "labor_pct",
    "food": "food_cost_pct",
    "reviews": "avg_rating",
    "operations": "comp_rate",
}

NOT_MEASURABLE = {
    "bar": "Cavnar AI has no pour-cost or bar-variance data — this was sized "
           "from what you told us, and nothing here can check it.",
    "waitlist": "Waitlist and turn-time are not modules Cavnar AI runs today.",
    "marketing": "Marketing has no revenue metric — reach and clicks are "
                 "measured, revenue attribution is not.",
    "technology": "Software savings are a billing question, not something "
                  "measured from your operations.",
}

CAVEAT = ("The audit estimate was a range built from what you reported that day. "
          "These are measurements taken before and after, not proof Cavnar AI "
          "moved them — a year carries a lot of other changes with it.")

# How long a window to read each side over. The audit is a point in time; a
# single day either side of it would be noise.
WINDOW_DAYS = 28


def link(audit_id, restaurant_id, db_path=None):
    """Point an audit at the account it became. Idempotent."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        conn.execute("UPDATE sales_audits SET linked_restaurant_id=?, updated_at=datetime('now') "
                     "WHERE id=?", (int(restaurant_id) if restaurant_id else None, audit_id))
        conn.commit()
    finally:
        conn.close()
    return True


def linked_audit(restaurant_id, db_path=None):
    """The most recent audit linked to this restaurant, or None."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, audit_date, results_json, restaurant_name FROM sales_audits "
            "WHERE linked_restaurant_id=? AND archived_at IS NULL AND results_json IS NOT NULL "
            "ORDER BY audit_date DESC, id DESC LIMIT 1", (restaurant_id,)).fetchone()
    except Exception as e:
        log.warning("promise: audit lookup failed for %s: %s", restaurant_id, e)
        return None
    finally:
        conn.close()
    if not row:
        return None
    import json
    try:
        results = json.loads(row["results_json"] or "{}")
    except Exception:
        return None
    return {"id": row["id"], "audit_date": row["audit_date"],
            "restaurant_name": row["restaurant_name"], "results": results}


def _measure_around(restaurant_id, metric, on_day, db_path):
    """The metric over the WINDOW_DAYS ending on `on_day`."""
    try:
        end = date.fromisoformat(str(on_day)[:10])
    except (TypeError, ValueError):
        return None, "no usable date"
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return metrics.measure(restaurant_id, metric, start.isoformat(), end.isoformat(), db_path)


def compare(restaurant_id, today=None, db_path=None):
    """The audit's estimate against the measurements, category by category."""
    from models import DB_PATH
    db_path = db_path or DB_PATH
    today = today or date.today()
    audit = linked_audit(restaurant_id, db_path=db_path)
    if not audit:
        return {"available": False,
                "reason": "no sales audit is linked to this account"}

    cats = (audit["results"] or {}).get("categories") or {}
    rows = []
    for key, cat in cats.items():
        entry = {"key": key, "label": cat.get("label") or key.title(),
                 "status": cat.get("status"),
                 "promised_low": cat.get("low"), "promised_likely": cat.get("likely"),
                 "promised_high": cat.get("high"),
                 # Only a band the audit computed for a sized category (R14,
                 # B1 H6): a "none" or unsized area carries no band, and a
                 # value outside the engine's three is not carried forward.
                 "confidence": (cat.get("confidence")
                                if cat.get("status") == "ok"
                                and cat.get("confidence") in ("low", "moderate", "high") else None)}
        if cat.get("status") == "insufficient":
            entry["state"] = "not_sized"
            entry["note"] = "The audit could not size this — not enough was known that day."
            rows.append(entry)
            continue
        if key in NOT_MEASURABLE:
            entry["state"] = "not_measurable"
            entry["note"] = NOT_MEASURABLE[key]
            rows.append(entry)
            continue
        metric = CATEGORY_METRIC.get(key)
        if not metric:
            entry["state"] = "not_measurable"
            entry["note"] = "No metric in this product maps to that category."
            rows.append(entry)
            continue

        then_v, then_why = _measure_around(restaurant_id, metric, audit["audit_date"], db_path)
        now = metrics.trailing(restaurant_id, metric, days=WINDOW_DAYS, end=today, db_path=db_path)
        info = metrics.describe(metric)
        entry.update({"metric": metric, "metric_label": info["label"], "unit": info["unit"],
                      "then": then_v, "then_detail": then_why,
                      "now": now["value"], "now_detail": now["detail"]})
        if then_v is None or now["value"] is None:
            entry["state"] = "unknown"
            # Which SIDE is missing matters: "we weren't syncing your data
            # back then" and "we aren't now" are different problems.
            entry["note"] = ("Not measurable at the time of the audit — "
                             f"{then_why}." if then_v is None else
                             f"Not measurable right now — {now['detail']}.")
            rows.append(entry)
            continue
        cmp = metrics.compare(metric, then_v, now["value"])
        entry.update(cmp)
        entry["state"] = cmp["verdict"]
        entry["monthly_dollars"] = (
            metrics.monthly_dollars(restaurant_id, metric, cmp["delta"], db_path)
            if cmp["verdict"] in ("improved", "worsened") else None)
        rows.append(entry)

    totals = (audit["results"] or {}).get("totals") or {}
    return {"available": True, "audit_id": audit["id"], "audit_date": audit["audit_date"],
            "categories": rows,
            "promised_annual": {"low": (totals.get("annual") or {}).get("low"),
                                "high": (totals.get("annual") or {}).get("high")},
            "measured_monthly": round(sum(abs(r["monthly_dollars"]) for r in rows
                                          if r.get("state") == "improved"
                                          and r.get("monthly_dollars")), 2),
            "caveat": CAVEAT}


def lines(cmp) -> list:
    """Plain sentences, for an email or a brief."""
    if not cmp.get("available"):
        return []
    out = []
    for r in cmp["categories"]:
        if r["state"] in ("not_sized", "not_measurable", "unknown"):
            out.append(f"{r['label']}: {r.get('note')}")
            continue
        unit = r.get("unit") or ""
        fmt = (lambda v: f"${v:,.0f}") if unit == "$" else (lambda v: f"{v:g}{unit}")
        word = {"improved": "better", "worsened": "worse",
                "no_clear_change": "in line with where it was"}.get(r["state"], r["state"])
        money = ""
        if r.get("monthly_dollars"):
            money = f" — about ${abs(r['monthly_dollars']):,.0f}/month"
        promised = ""
        if r.get("promised_low") or r.get("promised_high"):
            promised = (f" We estimated ${r['promised_low']:,.0f}–${r['promised_high']:,.0f} "
                        f"a year here.")
        out.append(f"{r['metric_label']}: {fmt(r['then'])} at the audit, "
                   f"{fmt(r['now'])} now — {word}{money}.{promised}")
    return out
