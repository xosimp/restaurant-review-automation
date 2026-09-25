"""Recommendation ROI audit, wave 2 — the web surfaces (templates/dashboard.html).

Asserted against the source, not a rendered fixture (a fixture only covers
the branches it happens to hit): the one reason picker behind every "Not
for us", the tracker replies said as the server gave them, the value tile
net of worse results with the cumulative total only when something was
measured, the owner's record page, the check-in, evidence opens, the answer
controls wherever the coverage contract says a line is answerable, and the
reply rate read from the server instead of a copy.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name, src=SRC):
    """The body of `function name(` up to the next top-level-ish function."""
    i = src.index("function " + name + "(")
    j = src.find("\nfunction ", i + 1)
    k = src.find("\n  function ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends) if ends else len(src)]


# ── #12, #11: one source for the reply rate; no Track on marketing ───────────

def test_the_review_savings_line_prices_replies_at_the_servers_rate():
    assert "_resp * 5" not in SRC and "responded x 5" not in SRC.replace("responded x 5 on every", "")
    body = _fn("cvReplyRate")
    assert "/api/value" in body and "reply_rate" in body
    assert "cvReplyRate(function(rate)" in SRC
    # The Reviews header's reply-writing caption is gone (density round #33:
    # Home's worth section owns that figure); no copy of the rate anywhere.
    assert "At $5 a reply" not in SRC and "of reply writing on" not in SRC


def test_marketing_is_not_offered_track():
    m = re.search(r"var REC_TRACKABLE=\{([^}]*)\};", SRC)
    assert m and "marketing" not in m.group(1) and "labor:1" in m.group(1)
    assert "REC_TRACKABLE[m]?" in SRC


# ── #22: one reason picker, six codes, every "Not for us" ────────────────────

def test_the_reason_picker_carries_the_six_codes_in_the_owners_words():
    import rec_ledger
    m = re.search(r"var REC_REASONS=\[(.*?)\];", SRC, re.S)
    codes = re.findall(r"\['(\w+)','", m.group(1))
    assert tuple(codes) == rec_ledger.REASON_CODES
    for words in ("Already doing this", "Doesn\\u2019t fit us", "Too costly", "Bad timing",
                  "Don\\u2019t trust the numbers", "Other"):
        assert words in m.group(1)
    picker = _fn("recReasonPicker")
    assert "Add a note (optional)" in picker and "data-rsn-skip" in picker and "data-rsn-cancel" in picker


def test_every_not_for_us_goes_through_the_picker_and_sends_the_code():
    # Module lines (recControlsHtml, the server's rec_controls_html, lone Not for us).
    handler = SRC[SRC.index("document.addEventListener('click',function(e){\n  var t=e.target&&e.target.closest?e.target.closest('[data-rec-key]')"):]
    assert "recAskWhy(t)" in handler[:400]
    answer = _fn("recAnswer")
    assert "body.reason_code=extra.reason_code" in answer
    # Home cards and the second hide on Needs attention (no more window.prompt).
    assert "window.prompt('You" not in SRC
    assert SRC.count("hbAskWhy(t,") == 2
    assert "if(reasonCode)body.reason_code=reasonCode;" in SRC
    # Ask's "Not now".
    ask = _fn("_askCavnarDismissWithReason")
    assert "recReasonPicker(row" in ask and "_recordAskCavnarAction(p, 'dismissed', note, code)" in ask
    assert "reason_code: (outcome === 'dismissed' && reasonCode) ? reasonCode : null" in SRC


# ── #4: tracker replies said the server's way ────────────────────────────────

def test_a_tracker_reply_is_said_as_measuring_until_or_its_reason():
    line = _fn("recTrackerLine")
    assert "'Measuring '" in line and "' until '" in line and "tracker_refused.reason" in line
    assert "window.mdy" in line                                     # M/D/YY
    assert "recAnswerSentence(ev,d)" in _fn("recAnswer")
    # Home's Done, Track this and one-tap reprice read the same reply.
    assert "d.tracker_refused){msg='Marked done. '+tl" in SRC
    assert "if(d.tracker_refused){if(typeof toast==='function')toast(d.tracker_refused.reason" in SRC
    assert "recTrackerLine(r)" in SRC


# ── #5: the value tile — net, cumulative, validated, never summed ────────────

def test_the_value_tile_nets_worse_results_and_shows_cumulative_only_when_measured():
    rv = _fn("renderValue")
    assert "d.net_monthly" in rv and "Measured, net" in rv and "wz.count" in rv
    # nothing measured is not $0: the cumulative line needs a total that is not null
    assert "hasCum=cum.total!==null&&cum.total!==undefined" in rv and "if(hasCum)" in rv
    assert "measured'+(ct<0?' net':'')+(cum.since?' since '+esc(mdy(cum.since))" in rv
    assert "d.validated>0" in rv and "d.net_note" in rv
    # Delivered, avoided, surfaced and opportunity stay four tiles; no sum.
    assert not re.search(r"(net|d\.monthly)\s*\+\s*(av\.|op\.|sf\.)", rv)


# ── #28 on Home, #13/#20/#35 the record page, #21 the check-in ───────────────

def test_home_reads_what_worked_and_the_timeline_for_the_check_in():
    assert "get('worked','/api/recs/what-worked?days=180')" in SRC
    assert "get('timeline','/api/recs/timeline?limit=40')" in SRC
    worked = _fn("renderWorked")
    assert "w.sentences" in worked and "Not enough yet" in worked


def test_the_record_page_reads_its_four_sources_and_pages_with_next_before():
    i = SRC.index('<div class="panel" id="panel-recs"')
    page = SRC[i:SRC.index("</script>", i)]
    for url in ("/api/recs/summary?days=", "/api/recs/what-worked?days=", "/api/recs/timeline?limit=25",
                "/api/outcomes", "&before='+encodeURIComponent(st.next)", "/abandon'"):
        assert url in page, url
    assert "m.enough&&isNum(m.accept_rate)" in page and "Not enough yet" in page       # a rate only with enough
    assert "Partial" in page and "A hint, not a result." in page                    # the interim, labelled
    assert "attribution_label" in page and "Also changed in those weeks" in page and "Validated" in page
    assert "Stop measuring" in page and "most_effective" in page
    assert "'#recs'" in page and 'data-rh="open"' in SRC


def test_the_check_in_asks_both_questions_and_rereads_the_result():
    cands = _fn("recCheckinCandidates")
    assert "o.owner_checkin" in cands and "it.tracker_id" in cands            # once per result, the ledger's join
    html = _fn("recCheckinHtml")
    assert "Did you make this change?" in html and "Did anything else change these weeks?" in html
    for a in ('data-ck="yes"', 'data-ck="partly"', 'data-ck="no"', 'data-ck2="1"'):
        assert a in html
    post = _fn("_recCheckinPost")
    assert "/api/recs/checkin" in post and "conditions_changed:!!changed" in post
    assert "attribution_label" in _fn("_recCheckinRefresh")


# ── #26, #41, #48, #38: answers wherever the contract says answerable ────────

@pytest.mark.parametrize("needle", [
    "ff.rec_key&&ff.answerable&&typeof recControlsHtml==='function'",           # the one thing
    "lkey&&l.answerable&&typeof recControlsHtml==='function'",                   # What connects
    "x.rec_key&&x.answerable&&typeof recControlsHtml==='function'",              # its other links
    "f.rec_key&&f.answerable&&typeof recControlsHtml==='function'?recControlsHtml(f.rec_key,'home','ops')",  # loss flags
    "rk=x.rec_key||x.key||''",                                                    # DSR actions by rec_key
    "x.answered?'<div class=\"ft\"><span class=\"rec-ans-done\">Answered</span></div>'",  # ...kept, without buttons
    "var cards = d.roadmap || [];",                                               # AI visibility from the server
    "recNotForUsHtml(c.rec_key, 'intel', 'intel')",
    "recNotForUsHtml(i.rec_key,'marketing','marketing')",                          # content ideas
    "recNotForUsHtml(mv.rec_key,'schedule_review','schedule')",                    # overtime moves
    "recControlsHtml(dg.rec_key,DIAG_SURFACE[id]||'labor'",                       # the labor diagnosis
    "recControlsHtml(sg[j].rec_key, 'ask', 'ask')",                               # Ask suggestions
    "'/api/ask-cavnar/feedback'",                                                 # Was this useful?
    # The streamed answer carries every field the server sends (message_id,
    # suggestions, confidence_detail ...), not a hand-picked list (B6#3).
    "for (var _k in evt) { if (evt.hasOwnProperty(_k) && _k !== 'type') _ans[_k] = evt[_k]; }",
])
def test_the_coverage_contract_has_its_web_controls(needle):
    assert needle in SRC, needle


def test_the_browser_no_longer_builds_the_ai_visibility_roadmap():
    assert "_impactRank" not in SRC and "var gbpDone" not in SRC


def test_evidence_opened_on_a_keyed_recommendation_is_recorded():
    ev = _fn("recEvidenceViewed")
    assert "event:'evidence_viewed'" in ev and "_recEvidenceSeen[key]" in ev
    # Home by default; a confidence Why? elsewhere names its own surface (J1).
    assert "data-explain-key" in SRC and ("recEvidenceViewed(t.getAttribute('data-explain-key'),"
                                          "t.getAttribute('data-explain-surface')||'home',"
                                          "t.getAttribute('data-explain-module')||'home')") in SRC
    assert 'data-evidence-key="\'+recEsc(r.key)+\'"' in SRC                     # Intel's cited reviews
    assert "recEvidenceViewed(key, 'intel', 'intel')" in SRC                     # the roadmap's reasoning


def test_the_new_surfaces_are_documented_in_the_design_system():
    ds = open(os.path.join(ROOT, "DESIGN_SYSTEM.md"), encoding="utf-8").read()
    for name in ("recReasonPicker", ".rck", "#panel-recs", "Measured alongside your changes", ".hb-vrows"):
        assert name in ds, name
