"""models.get_active_modules() — the single source of truth for "what
modules does this restaurant have," replacing the ad-hoc module_reviews/
module_labor/etc. boolean checks that used to be independently re-derived
in hosted_dashboard.py's index() and mobile_api.py's _do_mobile_home()."""
from models import Restaurant, get_active_modules


def _restaurant(**kw):
    return Restaurant(name=kw.pop("name", "Registry Test Co"), owner_email="r@x.com", **kw)


def test_no_modules_active_returns_empty_list():
    r = _restaurant(module_reviews=0, module_labor=0, module_inventory=0, module_marketing=0)
    assert get_active_modules(r) == []


def test_all_four_real_modules_plus_derived_intel():
    r = _restaurant(
        module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1,
        google_place_id="place123",
    )
    keys = [m["key"] for m in get_active_modules(r)]
    assert keys == ["reviews", "labor", "inventory", "marketing", "intel"]


def test_partial_module_set_only_returns_those():
    r = _restaurant(module_reviews=1, module_labor=0, module_inventory=1, module_marketing=0)
    keys = {m["key"] for m in get_active_modules(r)}
    assert keys == {"reviews", "inventory"}


def test_intel_requires_full_tier_and_google_place_id():
    # All 4 real modules on (full tier) but no connected GBP listing.
    r = _restaurant(module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    keys = {m["key"] for m in get_active_modules(r)}
    assert "intel" not in keys


def test_intel_requires_all_four_modules_even_with_place_id():
    # google_place_id set, but not full tier (marketing off) — Intel is a
    # full-tier bonus, not its own independent flag.
    r = _restaurant(
        module_reviews=1, module_labor=1, module_inventory=1, module_marketing=0,
        google_place_id="place123",
    )
    keys = {m["key"] for m in get_active_modules(r)}
    assert "intel" not in keys


def test_none_restaurant_returns_empty_list():
    assert get_active_modules(None) == []


def test_every_active_entry_has_available_status_by_default():
    r = _restaurant(module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    entries = get_active_modules(r)
    assert all(m["status"] == "available" for m in entries)


def test_future_module_reports_coming_soon_status_once_flagged():
    """module_waitlist/module_bar don't have real DB columns or Restaurant
    dataclass fields yet — simulate one existing (as it will once that
    column is migrated in) via setattr, confirming the registry entry
    correctly reports status='coming_soon' rather than 'available'."""
    r = _restaurant(module_reviews=0, module_labor=0, module_inventory=0, module_marketing=0)
    r.module_waitlist = 1  # simulates a future column/dataclass field
    entries = get_active_modules(r)
    assert len(entries) == 1
    assert entries[0]["key"] == "waitlist"
    assert entries[0]["status"] == "coming_soon"


def test_missing_future_module_attribute_is_treated_as_inactive():
    """Restaurants predating a module_waitlist/module_bar column must not
    error or accidentally activate — getattr's default handles this."""
    r = _restaurant(module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    keys = {m["key"] for m in get_active_modules(r)}
    assert "waitlist" not in keys
    assert "bar" not in keys
