"""Three of Simple EJ's nights with no summary (10/2, 10/3, 10/4/26), each
read from the model's real answer.

- 10/4: the lead stated ten figures on eleven cites; the cap was 10, the
  whole answer was refused, and the reason given was "has no cites".
- 10/2 and 10/3: the lead named a review's star rating ("one 5-star
  mention", "Joseph V, 2 stars") taken from tonight's review list without
  citing it, and the whole summary was held back.
"""
from dsr import READY
from dsr import narrative as n

LEAD_1004 = {
    "text": ("Net sales landed at $12,244, down 36.4% from yesterday's $19,260 but up 5.4% over last week's "
             "$11,617 and essentially on forecast (down 1.1%). Labor with salaries ran 39.7% of sales, 8.2 points "
             "over the 31.5% target, even though hourly labor alone sat under target at 28.1%, so the salaried "
             "share is where the real pressure is. Watch the salaried labor load and the evening hours mix against "
             "the lighter after-6pm sales."),
    "cites": ["sales.net", "sales.yesterday_net", "sales.vs_yesterday_pct", "sales.last_week_net",
              "sales.vs_last_week_pct", "sales.forecast_net", "sales.vs_forecast_pct", "labor.salaried_total_pct",
              "labor.salaried_vs_target_pts", "labor.target_pct", "labor.pct"]}


def _answer(lead):
    return {"executive_summary": lead, "went_well": [], "needs_attention": [], "actions_tomorrow": []}


def test_a_lead_with_eleven_cites_is_not_refused():
    clean, err = n.validate(_answer(LEAD_1004))
    assert err is None and len(clean["executive_summary"]["cites"]) == 11


def test_too_many_cites_says_so_rather_than_none():
    lead = dict(LEAD_1004, cites=["sales.k%d" % i for i in range(n.MAX_LEAD_CITES + 1)])
    _clean, err = n.validate(_answer(lead))
    assert err == "executive_summary has %d cites, more than the %d allowed" % (n.MAX_LEAD_CITES + 1,
                                                                               n.MAX_LEAD_CITES)
    _clean, err = n.validate(_answer(dict(LEAD_1004, cites=[])))
    assert "has no cites" in err


def _facts(reviews):
    return {"business_date": "2026-10-03", "blocks": {
        "sales": {"status": READY, "metrics": {"net": 19260.0, "vs_yesterday_pct": 28.3, "vs_forecast_pct": 8.3}},
        "labor": {"status": READY, "metrics": {"salaried_total_pct": 27.5, "salaried_vs_target_pts": -4.0,
                                               "target_pct": 31.5}},
        "service": {"status": READY, "metrics": {"loss_given_pct": 7.2}},
        "reviews": {"status": READY, "metrics": {"received": float(len(reviews)), "drafts_awaiting": 2.0},
                    "detail": {"reviews": reviews}}}}


def _lead_problem(text, cites, reviews):
    F = n.Facts(_facts(reviews))
    clean, err = n.validate(_answer({"text": text, "cites": cites}))
    assert err is None
    _body, _dropped, why = n.verify(clean, F)
    return why


CITES_1003 = ["sales.net", "sales.vs_yesterday_pct", "sales.vs_forecast_pct", "labor.salaried_total_pct",
              "labor.salaried_vs_target_pts", "service.loss_given_pct"]
KERRI = [{"id": 28434, "platform": "google", "rating": 5, "sentiment": "positive",
          "summary": "Staff member Kerri delivered excellent service.", "status": "drafted"}]


def test_a_star_rating_from_tonights_review_list_is_traced_to_it():
    text = ("Net sales hit $19,260, up 28.3% over last night and 8.3% above forecast, with labor including "
            "salaries at 27.5% of sales, 4 points under the 31.5% target. Service loss given sat at 7.2% of sales "
            "tonight. Reviews stayed clean with one 5-star review, but drafted replies are still sitting unposted.")
    assert _lead_problem(text, CITES_1003, KERRI) is None
    F = n.Facts(_facts(KERRI))
    assert "reviews.reviews" in F.complete_cites(text, CITES_1003)


def test_a_star_rating_no_review_tonight_holds_stays_untraced():
    text = ("Net sales hit $19,260, up 28.3% over last night and 8.3% above forecast. Labor including salaries "
            "ran 27.5% of sales. Reviews stayed clean with one 4-star review.")
    why = _lead_problem(text, CITES_1003, KERRI)
    assert why and "4-star" in why
    F = n.Facts(_facts(KERRI))
    assert "reviews.reviews" not in F.complete_cites(text, CITES_1003), "a 5 never backs a 4"


def test_completion_never_reads_a_star_out_of_any_other_list():
    text = "Net sales hit $19,260, up 28.3% over last night. Reviews stayed clean with one 5-star review."
    F = n.Facts(_facts([]))
    F.details["closeout.notes"] = [{"rating": 5}]
    assert "closeout.notes" not in F.complete_cites(text, ["sales.net", "sales.vs_yesterday_pct"])
