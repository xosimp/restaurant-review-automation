"""
bar/ — the Bar page (Food Cost → Bar): what the bar buys and what it earns,
from the distributor invoices Fintech sends (fintech_invoices) and the POS's
drink sales after comps and check discounts (bar.sales: the nightly report's
department and item-category dollars under the bar mapping, and the ticket
archive).

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
- sales      bar_sales / window_basis / _sales: POS drink sales after comps and
             check discounts under the bar mapping, gaps named;
             derive_nights (the archive's nights, net of discounts)
- mapping    the bar mapping (bar_sales_map, bar_sales_nights): suggest,
             Mapping, mapping_payload, save_mapping
- pour_cost  range_for, RULE_OF_THUMB, _window, _gaps, _total, _judge,
             window_span, _pour_cost, _trend ("Bought vs sold" until two
             counts bracket the window, then "Pour cost (counted)")
- judge      the one judgement policy (judge, freshness, stock_up, houses,
             stopped, confidence) and the owner's "we stopped buying from X"
             (bar_stopped_distributors, init_bar_judge; 10/15/26, not on the
             facade)
- targets    owner_targets, target_details: the owner's own alcohol cost
             targets through thresholds.bar_target_for
- prices     analyse, _price_watch: credit pairs, quantity tiers and
             ladders, price regimes, deals, forward money, floors,
             tier flags, split case (10/15/26, V2/V3/V15/V23/V24/V34/F8)
- spend      _distributors, _products, deposits, rebates_and_free_goods,
             mixers (F7, V33, Decision 3)
- products   the canonical product (valid UPC, codes, owner links), the pack
             parser, unit_costs (per bottle / oz / pour), reorder_hints, the
             owner's bar_settings / packs / links / deal sheet / drink map
             (init_bar_products, boot only, from init_bar_cost; F3, F8, F13)
- margins    the drink margin check (F11): drink_margin, margins,
             set_drink_map
- insights   _insights
- counts     bar_counts (init_bar_counts), save/delete/history, true_pour_cost,
             counted, latest_counted, the Back Office valuation reader, the
             month-end count reminder (10/15/26, F1; not on the facade)
- build      build: the page's payload
- variance   bought vs rung / variance per product (F2), keg yield (F14),
             bar_drink_specs (cocktail recipe cards as suggestions;
             init_bar_variance, boot only), bar_blocks -> `variance`,
             `bar_ops` (10/15/26; not on the facade)
- waste      bar_waste (init_bar_waste): log_waste, delete_waste,
             waste_rows, waste_block (F14)
- comps      comps and voids by bartender shift, owner-only: comps_block,
             for_viewer (F12)
- feed       feed_status (the payload's `feed`: invoices through, drink sales
             through, the last Fintech file, stale_days) and invoices_through

`bar_cost.py` stays as the facade every caller imports (`client_api`,
`models.init_db`, the tests): it re-exports every name above. Modules here
import each other absolutely (`from bar.common import ...`) and never import
`bar_cost` (tests/test_bar_package.py). A test that patches a helper patches
it on the module that owns it — a patch on the `bar_cost` facade does not
reach the package's own calls.
"""
