"""
bar/common.py — what every part of the Bar page shares: the categories and
their labels, the windows and limits the reads use, the call-time get_conn,
and the small formatters (M/D/YY dates, whole dollars, readable product
names). No reads of its own beyond get_conn.
"""
from datetime import date

import models as _models

CATEGORIES = ("beer", "liquor", "wine", "na", "supplies", "fee", "deposit", "other")
LABELS = {"beer": "Beer & seltzer", "liquor": "Liquor", "wine": "Wine", "na": "Non-alcoholic",
          "supplies": "Bar supplies", "fee": "Fees & taxes", "deposit": "Keg deposits & credits",
          "other": "Not sorted yet"}
# The categories pour cost is read for (each one's rule-of-thumb range is
# bar.pour_cost.RULE_OF_THUMB).
POUR_CATEGORIES = ("beer", "liquor", "wine")
WINDOW_DAYS = 28
PRICE_LOOKBACK_DAYS = 120
# The four-week pour cost read week by week: at most this many weekly points.
TREND_POINTS = 12
# A four-week reading this far from the one four weeks earlier is a change
# worth saying (purchase-based pour cost moves a point or so on its own).
TREND_SAY_POINTS = 1.5
# How many insights the page leads with.
MAX_INSIGHTS = 4

_UOM_WORD = {"CA": "case", "CS": "case", "KE": "keg", "KG": "keg", "BO": "bottle", "BL": "bottle", "BT": "bottle",
             "EA": "each", "DS": "deposit"}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _mdy(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return f"{d.month}/{d.day}/{str(d.year)[2:]}"


def display_name(description) -> str:
    """A distributor's all-caps description, easier to read: words of four
    or more letters in title case; short words (VDK, TEQ, BBL, IPA), sizes
    (750ML, 1/2) and codes stay as written."""
    out = []
    for w in str(description or "").split():
        out.append(w[:1].upper() + w[1:].lower() if len(w) >= 4 and w.isalpha() else w)
    return " ".join(out)


def _money(v) -> str:
    n = round(abs(float(v or 0)))
    return ("-" if float(v or 0) < 0 else "") + f"${n:,.0f}"
