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


PEOPLE = [Fact("labor.ot_premium", 65.88, "$", "measured", "week", entity="Slaone Arado"),
          Fact("labor.hours", 47.8, "h", "measured", "week", entity="Slaone Arado"),
          Fact("sales.day", 4200, "$", "measured", "day", entity="9/23"),
          Fact("labor.ot_premium", 40.10, "$", "measured", "week", entity="Gideon Kopalchick")]


def _p(text):
    return rv.validate(text, Ctx(restaurant_id=1, surface="labor_insight", facts=PEOPLE)).codes


def test_a_date_beside_a_persons_figure_qualifies_it_never_takes_it():
    """Live labor read, 10/2/26: "Slaone Arado's 47.8hr week (9/23) — highest
    single overtime premium on file, $65.88" was flagged "attached to the
    wrong day" because (9/23) sat nearest. Each kind binds to its own kind."""
    assert "F2" not in _p("Check Slaone Arado's 47.8hr week (9/23) — highest single overtime premium on file, $65.88.")
    assert "F2" in _p("Check Gideon Kopalchick's week (9/23) — overtime premium $65.88.")
    assert "F2" in _p("9/23 brought in $40.10.")
