"""The "Unverified" box names the claim it means (owner, 9/28/26).

Simple EJ's labor read flagged one causal overreach - "that day alone drives
the gap" (Tuesday ran 87.9%, but the other days still ran 53.5% without it)
- and the box read "Could not confirm Unsupported cause: nothing measured
here shows this caused it against your actual numbers": no claim named, the
sentence garbled, and the measured Tuesday trend read as unverified too."""
import client_api
import response_validation as rv
from response_validation import Fact, ValidationContext as Ctx, validate

READ = ("Erik, overall labor is running 56.4% against your 35% target. "
        "Cut Tuesday staffing hours across the board — that day alone drives the gap, especially 9/15.")


def test_the_caveat_quotes_the_clause_it_is_about():
    ctx = Ctx(restaurant_id=5, surface="labor_insight",
              facts=[Fact("labor.pct", 56.4, "%"), Fact("labor.target_pct", 35, "%")],
              policy={"action": "labor_insight"})
    v = validate(READ, ctx)
    assert "K1" in v.codes
    assert "A cause here isn't shown by your numbers: “that day alone drives the gap”." in v.actions["caveats"]


def test_the_phrase_stays_inside_its_own_clause():
    s = "Ratings fell because the patio closed, and Fridays ran short."
    i = s.index("because")
    assert rv._cause_phrase(s, i, i + len("because")) == "Ratings fell because the patio closed"


def test_the_box_shows_a_caveat_sentence_as_written_and_a_figure_list_inside_its_frame():
    html = client_api.format_insight_html(
        "Labor ran 56.4%.\nRecommendations:\n1. Cut Tuesday hours.\n\n"
        "UNVERIFIED: A cause here isn't shown by your numbers: “that day alone drives the gap”.")
    assert "Could not confirm" not in html and "against your actual numbers" not in html
    assert "A cause here isn&#39;t shown by your numbers: “that day alone drives the gap”." in html
    old = client_api.format_insight_html("Labor ran 56.4%.\n\nUNVERIFIED: $145, 38%")
    assert "Couldn’t confirm $145, 38% against your actual numbers." in old


def test_a_question_about_what_drove_a_night_is_not_a_claimed_cause():
    """Ask, 9/30/26: pointing Erik at the DSR to "see what drove it (low
    sales, overstaffing, or both)" read as an Unsupported cause. The claim
    it guards against still reads as one."""
    ctx = Ctx(restaurant_id=5, surface="ask", facts=[Fact("labor.pct", 69.5, "%"), Fact("labor.target_pct", 35, "%")],
              policy={"action": "ask"})
    ask = ("On 9/15/26 labor came in at 69.5% of sales. Worth pulling up that night's DSR if you want to see "
           "what drove it (low sales, overstaffing, or both).")
    assert "K1" not in validate(ask, ctx).codes
    assert "K1" in validate("Labor came in at 69.5% of sales — overstaffing drove it.", ctx).codes
