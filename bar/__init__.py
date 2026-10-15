"""
bar/ — the Bar page (Food Cost → Bar): what the bar buys and what it earns,
from the distributor invoices Fintech sends (fintech_invoices) and the
nightly report's sales by category (dsr_metrics `sales.cat:*`).

Two reads, both measured, neither a model's: the PRICE WATCH (every product
bought more than once, its latest price change and what it does at the
quantities the bar actually buys) and POUR COST BY CATEGORY (purchases
against the POS's sales for beer & seltzer, liquor and wine, over four
weeks and by month).

Module map (split from bar_cost.py 10/15/26, no behaviour change; each
module has one owner in a fix round):

- common     constants (CATEGORIES, LABELS, POUR_CATEGORIES, WINDOW_DAYS,
             PRICE_LOOKBACK_DAYS, TREND_POINTS, TREND_SAY_POINTS,
             MAX_INSIGHTS, _UOM_WORD), the call-time get_conn, _mdy,
             _money, display_name
- classify   the description regexes and distributor houses, product_key,
             classify, set_category, init_bar_cost (bar_product_categories,
             boot only)
- purchases  _lines: the invoice lines, sorted and netted
- sales      _sales: the nightly report's sales by bar category
- pour_cost  range_for, RULE_OF_THUMB, _window, _gaps, _total, _judge,
             window_span, _pour_cost, _trend ("Bought vs sold" until two
             counts bracket the window, then "Pour cost (counted)")
- judge      the one judgement policy (judge, freshness, stock_up, houses,
             stopped, confidence) and the owner's "we stopped buying from X"
             (bar_stopped_distributors, init_bar_judge; 10/15/26, not on the
             facade)
- targets    owner_targets, target_details: the owner's own alcohol cost
             targets through thresholds.bar_target_for
- prices     _price_watch
- spend      _distributors, _products
- insights   _insights
- counts     bar_counts (init_bar_counts), save/delete/history, true_pour_cost,
             counted, latest_counted, the Back Office valuation reader, the
             month-end count reminder (10/15/26, F1; not on the facade)
- build      build: the page's payload
- feed       feed_status (the payload's `feed`: invoices through, drink sales
             through, the last Fintech file, stale_days) and invoices_through

`bar_cost.py` stays as the facade every caller imports (`client_api`,
`models.init_db`, the tests): it re-exports every name above. Modules here
import each other absolutely (`from bar.common import ...`) and never import
`bar_cost` (tests/test_bar_package.py). A test that patches a helper patches
it on the module that owns it — a patch on the `bar_cost` facade does not
reach the package's own calls.
"""
