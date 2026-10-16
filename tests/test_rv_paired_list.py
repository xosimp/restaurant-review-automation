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


# EJ's labor read, 10/16/26: two false "attached to the wrong day" flags.
DAYS = [Fact("labor.pct", 56.5, "%", "measured", "period", entity="Wednesday"),
        Fact("labor.pct", 52.8, "%", "measured", "period", entity="Thursday"),
        Fact("labor.pct", 52.5, "%", "measured", "period", entity="Monday"),
        Fact("labor.target", 35.5, "%", "plan"),
        Fact("labor.avg_rate", 11.2, "", "measured", entity="Tuesday")]


def _d(text, prompt=""):
    return rv.validate(text, Ctx(restaurant_id=1, surface="labor_insight", facts=DAYS, context_text=prompt)).codes


def test_a_paired_run_binds_in_order_beside_another_figure_in_the_sentence():
    """'(56.5% and 52.8% labor) — both well above the 35.5% target': three
    figures, two days - the run after the days pairs, the target is apart."""
    assert "F2" not in _d("Review Wednesday and Thursday staffing (56.5% and 52.8% labor) — both well above the 35.5% target.")
    assert "F2" in _d("Review Wednesday and Thursday staffing (52.8% and 56.5% labor) — both well above the 35.5% target.")


def test_a_count_of_reviews_is_never_a_days_figure():
    """'Tuesdays and Mondays (11 reviews)' matched Tuesday's 11.2 within a
    count's half-unit and read as attached to Monday. A count of reviews is
    backed by a figure bound to nothing - the prompt's - or by nothing."""
    text = "Guests complain about service on Tuesdays and Mondays (11 reviews) — watch Monday's 52.5% labor next."
    codes = _d(text, prompt="Service complaints: 11 reviews mention slow service on Mondays and Tuesdays.")
    assert "F2" not in codes and "F1" not in codes
    assert "F1" in _d(text), "with no such count anywhere, it is not in the data - never 'the wrong day'"
