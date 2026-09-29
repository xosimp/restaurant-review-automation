"""A paired list binds in order (live labor read, 9/29/26): "9/3 and 9/4 ran
well below target on strong sales ($15,399 and $13,815)" was flagged
"attached to the wrong day" because $15,399 sat nearest to 9/4 — though it
was 9/3's, correctly. The nearest-name rule still catches a real swap."""
import response_validation as rv
from response_validation import Fact, ValidationContext as Ctx

FACTS = [Fact("sales.day", 15399.44, "$", "measured", "day", entity="9/3"),
         Fact("sales.day", 13815.07, "$", "measured", "day", entity="9/4")]


def _v(text):
    return rv.validate(text, Ctx(restaurant_id=1, surface="labor_insight", facts=FACTS))


def test_a_paired_list_binds_each_figure_to_its_own_day():
    v = _v("9/3 and 9/4 ran well below target on strong sales ($15,399 and $13,815).")
    assert "F2" not in v.codes, v.findings


def test_a_swapped_paired_list_is_still_caught():
    v = _v("9/3 and 9/4 ran well below target on strong sales ($13,815 and $15,399).")
    assert "F2" in v.codes


def test_the_nearest_name_rule_still_holds_outside_a_list():
    assert "F2" in _v("9/4 brought in $15,399.").codes
    assert "F2" not in _v("9/3 brought in $15,399 and 9/4 brought in $13,815.").codes
