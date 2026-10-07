"""AI visibility asks the Google searches that bring people to the site
(Search Console), and reports Google vs AI by search volume (10/7/26)."""
import models
import web_analytics as wa
from tests.test_intel_integrity import _payload

EJS = "Simple EJ's Kitchen & Tap"


def _searches(db_path, rid, rows):
    conn = models.get_conn(db_path)
    conn.executemany("INSERT INTO web_search_queries (restaurant_id, window_end, query, clicks, impressions, position) "
                     "VALUES (?,?,?,?,?,?)", [(rid, "2026-10-06", q, c, i, p) for q, c, i, p in rows])
    conn.commit()
    conn.close()


EJS_SEARCHES = [
    ("restaurants near me", 62, 1836, 6.6), ("food near me", 27, 1568, 7.2), ("simple ejs", 614, 962, 1.0),
    ("st charles restaurants", 33, 869, 3.2), ("restaurants st charles il", 32, 577, 1.5),
    ("restaurants in st charles il", 26, 514, 1.3), ("ejs", 104, 426, 3.9), ("simply ejs", 73, 133, 1.0),
    ("ejs sports bar", 33, 76, 1.8), ("st charles il", 5, 40, 9.0), ("tiny", 1, 3, 20.0),
]


def test_the_restaurants_own_name_is_never_a_discovery_question():
    for q in ("simple ejs", "simply ejs", "ejs", "ej st charles", "ejs sports bar", "Simple EJ's menu"):
        assert wa.is_branded(q, EJS), q
    for q in ("restaurants near me", "food near me", "st charles restaurants", "best bar food near me"):
        assert not wa.is_branded(q, EJS), q
    # A word that says what the place is, not who it is, is not the brand
    assert not wa.is_branded("pizza near me", "Gia Mia Pizza Bar")


def test_a_search_becomes_the_question_a_guest_would_ask_an_ai():
    f = lambda q: wa.to_ai_question(q, "St. Charles", "St. Charles, IL")
    assert f("restaurants near me") == "restaurants near St. Charles, IL"
    assert f("st charles restaurants") == "restaurants in St. Charles, IL"
    assert f("restaurants st charles il") == "restaurants in St. Charles, IL"
    assert f("wings") == "wings in St. Charles, IL"
    assert f("st charles il") is None          # only the place: nothing to ask


def test_phrasings_of_one_search_merge_and_branded_and_thin_ones_drop(db_path):
    """To an AI "restaurants near St. Charles, IL" and "restaurants in St.
    Charles, IL" are one question: EJ's four searches for it merge, worded as
    the most-searched one, with their volume summed."""
    _searches(db_path, 5, EJS_SEARCHES)
    qs = wa.ai_questions(5, EJS, "St. Charles", "St. Charles, IL", db_path=db_path)
    assert [q["q"] for q in qs] == ["restaurants near St. Charles, IL", "food near St. Charles, IL"]
    top = qs[0]["search"]
    assert top["impressions"] == 1836 + 869 + 577 + 514
    assert set(top["queries"]) == {"restaurants near me", "st charles restaurants", "restaurants st charles il",
                                   "restaurants in st charles il"}
    assert 1.3 <= top["position"] <= 6.6 and all(q["kind"] == "search" for q in qs)


def test_no_search_console_data_means_the_fixed_questions_as_before(db_path):
    assert wa.ai_questions(9, EJS, "St. Charles", "St. Charles, IL", db_path=db_path) == []


def test_the_check_asks_the_searches_first_and_reports_google_vs_ai(db_path, monkeypatch):
    _searches(db_path, 1, [("restaurants in geneva il", 30, 900, 1.4), ("food near me", 20, 600, 6.0),
                           ("gia mia", 300, 800, 1.0)])
    sent = []
    # Named in the first answer only.
    answers = ["Gia Mia in Geneva is a favourite."] + ["Try Alter Brewing in Geneva."] * 11
    p = _payload(monkeypatch, db_path, answers=answers, sent=sent, city="Geneva", state="IL")
    qs = p["queries"]
    assert len(qs) == 8 and qs[0]["kind"] == "search" and qs[0]["search"]["impressions"] == 900
    assert all(q["kind"] != "search" for q in qs[2:]) and qs[-1]["kind"] == "branded"
    assert not any(q["query"].startswith("Top restaurants in") for q in qs)   # the search covers it
    sd = p["search_demand"]
    assert sd["questions"] == 2 and sd["impressions"] == 1500
    assert sd["named"] == 1 and sd["covered_pct"] == 60


def test_intel_shows_google_vs_ai_on_the_question_cards():
    """The card for a question that came from a Google search says how it does
    on Google; a miss names who AI recommended and which sites it read."""
    import json, re, shutil, subprocess, pathlib
    import pytest
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    s = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()

    def grab(sig):
        a = s.index(sig); i = s.index("{", a); depth = 0
        for j in range(i, len(s)):
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            if depth == 0:
                return s[a:j + 1]
    harness = r"""
var els={};function mk(id){return els[id]||(els[id]={innerHTML:'',textContent:'',hidden:true,style:{},className:'',title:'',
 setAttribute:function(){},getAttribute:function(){return null},querySelector:function(){return null},querySelectorAll:function(){return []},
 classList:{add:function(){},remove:function(){},toggle:function(){}},addEventListener:function(){}});}
var document={getElementById:mk,querySelector:function(){return null},querySelectorAll:function(){return []},createElement:function(){return mk('x')}};
var window={};function esc(x){return String(x==null?'':x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function _aivFitBody(){}function _in2ChevronDown(){return ''}function aivLoadingHide(){}function cavSet(){}function in2PaintQHist(){}function countUp(){}
"""
    d = {"ok": True, "ai_score": 50, "checklist": [], "search_demand": {"questions": 1, "named": 0, "impressions": 3796,
                                                                       "covered_pct": 0, "basis": "b"},
         "queries": [{"query": "restaurants near St. Charles, IL", "kind": "search", "appeared": False, "ok": True,
                      "answer": "Try Alter.", "competitors_named": ["Alter Brewing"],
                      "sources": ["https://www.tripadvisor.com/x", {"url": "https://www.opentable.com/y"}],
                      "search": {"queries": ["restaurants near me", "st charles restaurants"], "impressions": 3796,
                                 "position": 4.1}}]}
    js = (harness + grab("function aivMeasured(d) {") + grab("function aivRangeReading(ai, lo, hi) {")
          + grab("function renderAIVisibility(d) {")
          + "\nrenderAIVisibility(" + json.dumps(d) + ");console.log(els['in2-gva'].innerHTML+'\\n'+els['aiv-queries'].innerHTML);")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    html = out.stdout
    assert "From your Google searches" in html and "about <b class=\"hb-num\">#4</b>" in html
    assert "3,796" in html and "AI named instead: Alter Brewing" in html
    assert "AI read: tripadvisor.com, opentable.com" in html and "0%</b> of that search volume" in html
