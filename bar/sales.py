"""
bar/sales.py — what the POS sold by bar category, from the nightly reports'
`sales.cat:*` metrics (dsr_metrics).
"""
from collections import defaultdict

from bar.common import get_conn


def _sales(restaurant_id, db_path=None) -> dict:
    """{category: {date: sales}} from the nightly reports' sales by category."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                            "AND metric LIKE 'sales.cat:%' AND value IS NOT NULL", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = defaultdict(dict)
    for r in rows:
        name = str(r["metric"]).split(":", 1)[1].lower()
        if "beer" in name or "seltzer" in name:
            cat = "beer"
        elif "liquor" in name or "spirit" in name or "cocktail" in name:
            cat = "liquor"
        elif "wine" in name:
            cat = "wine"
        else:
            continue
        d = str(r["business_date"])[:10]
        out[cat][d] = out[cat].get(d, 0.0) + float(r["value"] or 0)
    return out
