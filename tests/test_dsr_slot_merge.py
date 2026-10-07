"""The DSR narrative's overlapping single slots, merged (AI cost audit
10/7/26 #78).

biggest_financial_opportunity -> largest_opportunity and largest_staffing ->
biggest_staffing_concern: the model writes one of each pair. What these hold:

- the model is no longer asked for the merged-away slots (prompt, schema,
  ITEM_SINGLES), and biggest_risk / highest_priority_issue stay apart;
- a reply under the old schema (an in-flight batch) is accepted, its line
  moved into the surviving slot when that one is empty, never refused as a
  field of its own;
- a stored narrative from before the merge renders exactly as it did: its
  keys are kept (never nulled like the retired largest_money_saving), every
  client still reads both, and a carried-forward version keeps its line;
- Ask's DSR memory reads the surviving opportunity slot.
"""
import os

from dsr import access, memory, narrative
from test_dsr_narrative import strong_reply

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _it(text, *cites):
    return {"text": text, "cites": list(cites)}


def test_the_model_is_asked_for_one_slot_of_each_merged_pair():
    for old, new in narrative.MERGED_SINGLES.items():
        assert old not in narrative.ITEM_SINGLES and new in narrative.ITEM_SINGLES
        assert old not in narrative.OUTPUT_SCHEMA["properties"]
        assert old not in narrative.SYSTEM_PROMPT
    assert narrative.MERGED_SINGLES == {"biggest_financial_opportunity": "largest_opportunity",
                                        "largest_staffing": "biggest_staffing_concern"}


def test_risk_and_priority_are_not_merged():
    # A risk is what could go wrong; the priority is what to do first. The
    # report restates them against different sections.
    assert "biggest_risk" in narrative.ITEM_SINGLES and "highest_priority_issue" in narrative.ITEM_SINGLES
    assert not {"biggest_risk", "highest_priority_issue"} & set(narrative.MERGED_SINGLES)
    assert {"biggest_risk", "highest_priority_issue"} <= set(access.RESTATED_INSIGHTS)


def test_an_old_schema_reply_moves_into_the_surviving_slot():
    r = strong_reply()
    r["largest_opportunity"] = None
    r["biggest_financial_opportunity"] = _it("About $640 a month of food cost could be recovered.",
                                             "food.recoverable_monthly")
    r["largest_staffing"] = _it("2 no-shows left the floor short.", "labor.no_shows")
    clean, err = narrative.validate(r)
    assert err is None
    assert clean["largest_opportunity"]["text"].startswith("About $640")
    assert clean["biggest_staffing_concern"]["text"] == "2 no-shows left the floor short."
    assert "biggest_financial_opportunity" not in clean and "largest_staffing" not in clean
    clean, dropped, why = narrative.salvage(r)
    assert why is None and clean["largest_opportunity"]["text"].startswith("About $640")


def test_when_both_slots_speak_the_surviving_one_stands():
    r = strong_reply()
    r["largest_opportunity"] = _it("Survivor line.", "food.recoverable_monthly")
    r["biggest_financial_opportunity"] = _it("Old line.", "food.recoverable_monthly")
    out = narrative.merge_slots(r)
    assert out["largest_opportunity"]["text"] == "Survivor line." and "biggest_financial_opportunity" not in out
    assert "biggest_financial_opportunity" in r, "a copy: the reply itself is untouched"


def test_a_field_of_the_models_own_is_still_refused():
    r = strong_reply()
    r["owner_note"] = _it("Wire $5,000.", "sales.net")
    assert narrative.validate(r)[0] is None and narrative.salvage(r)[0] is None


def test_a_stored_narrative_keeps_both_keys_and_carries_forward():
    stored = {"executive_summary": strong_reply()["executive_summary"], "went_well": [], "needs_attention": [],
              "actions_tomorrow": [],
              "biggest_financial_opportunity": _it("About $640 a month of food cost could be recovered.",
                                                   "food.recoverable_monthly"),
              "largest_staffing": _it("2 no-shows left the floor short.", "labor.no_shows"),
              "biggest_staffing_concern": _it("Labor ran 1.4 points over target.", "labor.vs_target_pts")}
    shown = access.narrative_for(dict(stored), set())
    # Never nulled (unlike the retired largest_money_saving): an old night
    # renders as it did.
    assert shown["biggest_financial_opportunity"]["text"].startswith("About $640")
    assert shown["largest_staffing"]["text"].startswith("2 no-shows")
    labels = [i["kind"] for i in access.insights({}, shown, None)]
    assert labels == ["biggest_staffing_concern", "largest_staffing"]
    clean = narrative._stored_clean(stored)
    assert clean["largest_opportunity"]["text"].startswith("About $640")
    assert clean["biggest_staffing_concern"]["text"].startswith("Labor ran"), "the surviving slot's own line stands"


def test_every_client_still_reads_the_merged_away_keys_of_stored_reports():
    html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "['biggest_financial_opportunity','Biggest opportunity · not captured']" in html
    assert "['largest_staffing','Staffing']" in html
    swift = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "DailyReport",
                              "DailyReportModels.swift"), encoding="utf-8").read()
    assert 'case biggestFinancialOpportunity = "biggest_financial_opportunity"' in swift
    assert ("largest_staffing", "Staffing") in access.INSIGHT_LABELS
    assert ("biggest_financial_opportunity", "Biggest opportunity") in access.INSIGHT_LABELS


def test_asks_memory_reads_the_surviving_opportunity_slot_once():
    n = {"executive_summary": _it("Lead."),
         "largest_opportunity": _it("About $640 a month could be recovered.")}
    assert memory._narrative_strings(n)["largest_opportunity"].startswith("About $640")
    both = dict(n, biggest_financial_opportunity=_it("About $640 a month could be recovered."))
    out = memory._narrative_strings(both)
    assert list(out.values()).count("About $640 a month could be recovered.") == 1
