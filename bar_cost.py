"""
bar_cost.py — the facade for the Bar page (Food Cost → Bar). The code lives
in the `bar/` package (split 10/15/26, no behaviour change; the module map is
in bar/__init__.py); this module re-exports every name it used to define, so
`import bar_cost as _bc` (client_api), `from bar_cost import init_bar_cost`
(models.init_db) and the tests keep working unchanged.

A patch on a name here does not reach the package's own calls: patch the
owning module (`bar.pour_cost._gaps`, `bar.common.get_conn`, ...).
"""
from bar.build import build
from bar.classify import (_BEER_HOUSES, _BEER_RE, _FEE_RE, _KEG_RE, _LIQUOR_RE, _NA_HOUSES, _NA_RE,
                          _SELTZER_RE, _SPIRIT_HOUSES, _SUPPLY_RE, _WINE_HOUSES, _WINE_RE, classify,
                          init_bar_cost, product_key, set_category)
from bar.common import (CATEGORIES, LABELS, MAX_INSIGHTS, POUR_CATEGORIES, PRICE_LOOKBACK_DAYS,
                        TREND_POINTS, TREND_SAY_POINTS, WINDOW_DAYS, _UOM_WORD, _mdy, _models, _money,
                        display_name, get_conn)
from bar.insights import _insights
from bar.pour_cost import RULE_OF_THUMB, _gaps, _judge, _pour_cost, _trend, _window
from bar.prices import _price_watch
from bar.purchases import _lines
from bar.sales import _sales
from bar.spend import _distributors, _products
from bar.targets import owner_targets

__all__ = [
    "build", "classify", "product_key", "set_category", "init_bar_cost", "owner_targets", "display_name",
    "get_conn", "CATEGORIES", "LABELS", "POUR_CATEGORIES", "RULE_OF_THUMB", "WINDOW_DAYS",
    "PRICE_LOOKBACK_DAYS", "TREND_POINTS", "TREND_SAY_POINTS", "MAX_INSIGHTS",
]
