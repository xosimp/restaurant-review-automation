"""inventory.build_price_watch() — unifies compute_item_trends()'s
price_alerts (single-week spike) and trend_alerts (3+ week rise) into one
display-ready list for the Price Watch UI."""
from inventory import build_price_watch


def _trends(price_alerts=None, trend_alerts=None):
    return {"price_alerts": price_alerts or [], "trend_alerts": trend_alerts or []}


def test_spike_only():
    trends = _trends(price_alerts=[
        {"item": "Roma Tomatoes", "old_price": 1.80, "new_price": 1.93, "change_pct": 7.2, "is_big_8": False}
    ])
    watch = build_price_watch(trends)
    assert len(watch) == 1
    assert watch[0]["kind"] == "spike"
    assert watch[0]["weeks"] is None
    assert "invoice" in watch[0]["action_hint"]


def test_trend_only_big_8_mentions_menu_pricing():
    trends = _trends(trend_alerts=[
        {"item": "Parmesan Cheese", "weeks": 3, "start_price": 8.00, "current_price": 9.45,
         "total_change_pct": 18.1, "is_big_8": True}
    ])
    watch = build_price_watch(trends)
    assert len(watch) == 1
    assert watch[0]["kind"] == "trend"
    assert watch[0]["weeks"] == 3
    assert "menu price" in watch[0]["action_hint"]


def test_trend_only_non_big_8_is_softer_hint():
    trends = _trends(trend_alerts=[
        {"item": "Napkins", "weeks": 3, "start_price": 0.10, "current_price": 0.12,
         "total_change_pct": 20.0, "is_big_8": False}
    ])
    watch = build_price_watch(trends)
    assert "keeping an eye" in watch[0]["action_hint"]
    assert "menu price" not in watch[0]["action_hint"]


def test_same_item_trend_wins_over_spike():
    trends = _trends(
        price_alerts=[{"item": "Salmon", "old_price": 12.0, "new_price": 13.0, "change_pct": 8.3, "is_big_8": True}],
        trend_alerts=[{"item": "Salmon", "weeks": 3, "start_price": 10.0, "current_price": 13.0,
                       "total_change_pct": 30.0, "is_big_8": True}],
    )
    watch = build_price_watch(trends)
    assert len(watch) == 1  # deduped, not two rows for the same ingredient
    assert watch[0]["kind"] == "trend"


def test_sorted_by_magnitude_descending():
    trends = _trends(
        price_alerts=[{"item": "Small Move", "old_price": 10.0, "new_price": 10.6, "change_pct": 6.0, "is_big_8": False}],
        trend_alerts=[{"item": "Big Move", "weeks": 3, "start_price": 5.0, "current_price": 8.0,
                       "total_change_pct": 60.0, "is_big_8": True}],
    )
    watch = build_price_watch(trends)
    assert [w["item"] for w in watch] == ["Big Move", "Small Move"]


def test_empty_trends_returns_empty_watch():
    assert build_price_watch(_trends()) == []
