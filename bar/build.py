"""
bar/build.py — the Bar page's payload, assembled from the package's reads.
"""
from datetime import date

from bar.common import LABELS, _mdy
from bar.counts import counted as _counted
from bar.insights import _insights
from bar.pour_cost import _pour_cost
from bar.prices import _price_watch
from bar.purchases import _lines
from bar.sales import bar_sales, window_basis
from bar.spend import _distributors, _products
from bar.targets import owner_targets


def build(restaurant_id, db_path=None, today=None) -> dict:
    """The Bar page's payload."""
    today = today or date.today()
    lines = _lines(restaurant_id, db_path)
    if not lines:
        return {"ok": True, "available": False,
                "why_not": "No distributor invoices yet. Once Fintech's daily file reaches Cavnar AI, "
                           "the bar's prices and pour cost fill in here."}
    # POS drink sales after comps and check discounts, under the bar mapping
    # as it stands (bar.sales); a night with a sales gap is left out of the
    # per-day figures and named below.
    sales_detail = bar_sales(restaurant_id, db_path)
    sales = {k: v for k, v in sales_detail["by_category"].items() if k in ("beer", "liquor", "wine")}
    inv_dates = sorted({str(l["invoice_date"])[:10] for l in lines})
    products = _products(lines)
    other_spend = round(sum(p["spend"] for p in products if p["category"] == "other"), 2)
    pour = _pour_cost(lines, sales, today, targets=owner_targets(restaurant_id, db_path))
    win = pour.get("window") if pour.get("available") else None
    sales_basis = window_basis(sales_detail, win["from"], win["to"]) if win else sales_detail["basis_all"]
    if win:
        # A night inside the window with net sales and no drink coverage is a
        # sales gap: the window is not complete (V5); the engine's judgement
        # reads `sales_gaps` (bar.pour_cost / bar.judge).
        pour["sales_gaps"] = sales_basis["gaps"]
        if sales_basis["gaps"]:
            pour["complete"] = False
    prices = _price_watch(lines, today)
    to_check = sum(1 for p in products if not (p["sure"] or p["owner_set"]))
    return {"ok": True, "available": True,
            "as_of": inv_dates[-1], "as_of_mdy": _mdy(inv_dates[-1]),
            "invoices": {"from_mdy": _mdy(inv_dates[0]), "to_mdy": _mdy(inv_dates[-1]),
                         "count": len({(l["distributor"], l["invoice_number"]) for l in lines})},
            "pour_cost": pour,
            # Counted pour cost from the bar's shelf counts (bar.counts, F1):
            # withheld with `missing` until two counts bracket a span.
            "counted": _counted(restaurant_id, today=today, db_path=db_path, lines=lines, sales=sales),
            "price_watch": prices,
            "insights": _insights(pour, prices, to_check),
            "distributors": _distributors(lines),
            "products": products,
            "to_check": to_check,
            "other_spend": other_spend,
            "categories": [{"key": c, "label": LABELS[c]} for c in ("beer", "liquor", "wine", "na", "supplies", "other")],
            "sales_basis": sales_basis,
            "basis": "Purchases are Fintech's distributor invoices; sales are the nightly report's sales by "
                     "category. Pour cost here is purchase-based (no bar count), read over four weeks and by "
                     "month; the ranges are industry rules of thumb."}
