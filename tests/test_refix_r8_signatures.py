"""Re-audit 9/29/26, CROSSMODULE-1 — a "not for us" to one lever silenced the
OPPOSITE advice from another module, in both directions.

"Trim Tuesday" (trim_day:Tuesday) and the marketing x labor link "hold any
cut to Tuesday while the campaign runs" had one advice signature,
labor:day:tuesday. Declining the trim dropped the hold link from the one
thing; declining the link silenced Labor's trim for a year. A staffing
signature now carries its direction when it keeps or adds people
(":hold", ":add"); a cut keeps the plain signature, so every stored cut
signature reads as before. The answer to one side is evidence FOR the other
— said beside it — never a silence of it.
"""
import pytest

import business_intelligence as bi
import insight_store as ist
import lever_conflicts
import models
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(bi, "get_conn", redirect, raising=False)
    ist._SUBJECTS_CACHE.clear()
    lever_conflicts._FACTS_CACHE.clear()
    yield


def _rid(name="Sig Dir Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


LAB = {"is_live": True, "period_days": 28, "date_range": {"days": 28}, "labor_target": 30,
       "potential_savings_monthly": 900.0,
       "dow_summary": {"Monday": 28, "Tuesday": 36, "Wednesday": 29, "Thursday": 28, "Friday": 27,
                       "Saturday": 26, "Sunday": 28}}


def _hold_link(rid):
    mk = {"posts_published": 0, "fill_campaigns": [{"day": "Tuesday", "sent": 412, "total": 412, "queued": False,
                                                     "on": bi._local_today(rid).isoformat()}]}
    links = [l for l in bi.correlations(rid, data={"labor": LAB, "marketing": mk})
             if l["kind"] == "marketing_x_labor"]
    assert links, "the fill campaign on the heaviest day is a link"
    return links[0]


def test_a_staffing_signature_carries_its_direction():
    assert ist.advice_signature("trim_day:Tuesday", "Trim Tuesday staffing") == "labor:day:tuesday"
    assert ist.advice_signature("link:marketing_x_labor:fill:tuesday",
                                "Hold any cut to Tuesday until the campaign's window closes") == \
        "labor:day:tuesday:hold"
    # The weekday is what a staffing link is about, not the complaint theme.
    assert ist.advice_signature("link:reviews_x_labor:service:friday", "Check Friday") == "labor:day:friday:add"
    assert ist.advice_signature("link:dsr_x_reviews:service:friday", "x") == "labor:day:friday:add"
    # A DSR staffing action reads its own words.
    assert ist.advice_signature("dsr_action:adjust_staffing:labor",
                                "Add a dishwasher Friday at 7pm") == "labor:day:friday:add"
    assert ist.advice_signature("dsr_action:adjust_staffing:labor",
                                "Cut a server from Tuesday dinner") == "labor:day:tuesday"
    # A cut kind stays a cut whatever its words say.
    assert ist.advice_signature("trim_day:Tuesday", "Trim Tuesday — hold the Friday crew") == "labor:day:tuesday"
    assert ist.split_signature("labor:day:tuesday:hold") == ("labor:day:tuesday", "hold")
    assert ist.split_signature("marketing:dish:steak: add") == ("marketing:dish:steak: add", None)


def test_declining_the_trim_leaves_the_hold_link_the_one_thing():
    """Proof 5, forward: the owner says "we never cut Tuesday"."""
    rid = _rid()
    rec_ledger.present_many(rid, [{"key": "trim_day:Tuesday", "module": "labor",
                                   "title": "Trim Tuesday staffing on the next schedule"}], "home")
    rec_ledger.record(rid, "trim_day:Tuesday", "dismissed", surface="home", meta={"kind": "not_for_us"})
    assert "labor:day:tuesday" in ist.declined_signatures(rid)
    link = _hold_link(rid)
    pick = bi.pick_one_thing(rid, bi.one_thing_candidates(rid, {}, [link]), log_rank=False)
    assert pick and pick["key"] == bi.link_key(link)
    assert pick["advice_signature"] == "labor:day:tuesday:hold"
    # ...and it says the owner's decline agrees with it, never silenced by it.
    assert pick["agrees_with"]["signature"] == "labor:day:tuesday"
    assert "agrees" in pick["agrees_with"]["text"]


def test_declining_the_hold_link_leaves_labors_trim():
    """The reverse: "not for us" on the hold link used to drop every Trim
    Tuesday on Home for a year."""
    rid = _rid()
    link = _hold_link(rid)
    key = bi.link_key(link)
    rec_ledger.present_many(rid, [{"key": key, "module": "home", "title": link["headline"]}], "home")
    rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"})
    declined = ist.declined_signatures(rid)
    assert "labor:day:tuesday:hold" in declined
    assert ist.advice_signature("trim_day:Tuesday", "Trim Tuesday staffing on the next schedule") not in declined


def test_a_hold_declined_before_signatures_had_a_direction_reads_with_it(db_path):
    """An answer stored with the old, direction-free signature is read with
    the direction its key gives it now — existing declines of a hold link
    stop silencing the trim at once."""
    rid = _rid()
    key = "link:marketing_x_labor:fill:tuesday"
    rec_ledger.present_many(rid, [{"key": key, "module": "home", "title": "A text to fill Tuesday went out"}], "home")
    rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"})
    conn = models.get_conn(db_path)
    conn.execute("UPDATE rec_instances SET signature='labor:day:tuesday' WHERE restaurant_id=? AND key=?", (rid, key))
    conn.commit()
    conn.close()
    declined = ist.declined_signatures(rid)
    assert "labor:day:tuesday:hold" in declined and "labor:day:tuesday" not in declined
    assert "labor:day:tuesday:hold" in ist.declines_by_signature(rid)


def test_a_hold_on_a_fill_night_is_not_a_trim_against_the_fill():
    hold = {"key": "link:marketing_x_labor:fill:tuesday", "title": "Hold any cut to Tuesday",
            "advice_signature": "labor:day:tuesday:hold", "score": 100}
    trim = {"key": "trim_day:Tuesday", "title": "Trim Tuesday", "advice_signature": "labor:day:tuesday", "score": 50}
    fill = {"key": "slow_day:Tuesday", "title": "Fill Tuesday", "advice_signature": "guest_outreach:day:tuesday",
            "score": 80}
    found = lever_conflicts.find([hold, fill])
    assert not found, "holding the crew on the night a campaign fills agrees with it"
    found = lever_conflicts.find([trim, fill])
    assert [f["rule"] for f in found] == ["trim_vs_fill"] and found[0]["id"] == "trim_vs_fill:labor:day:tuesday"


def test_the_decline_of_one_side_is_decisions_memory_for_the_other():
    import decisions
    rows = [{"key": "trim_day:Tuesday", "title": "Trim Tuesday staffing", "answer": "passed",
             "signature": "labor:day:tuesday", "reason_code": "doesnt_fit"}]
    picked = decisions.pick_relevant(rows, subjects=["labor:day:tuesday:hold"])
    assert picked and picked[0]["weight"] >= 100, "the owner's no to the trim is about the hold advice too"
