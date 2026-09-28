"""A competitor refresh loads like the AI-visibility check: the big searching
orb on the page, not a radar inside an ember-tinted card or a small orb in
the button (owner, 9/28/26)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    start = SRC.index("function " + name + "(")
    depth, i = 0, SRC.index("{", start)
    while True:
        if SRC[i] == "{":
            depth += 1
        elif SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:i + 1]
        i += 1


def test_the_refresh_shows_the_big_searching_orb_and_nothing_else():
    run = _fn("refreshCompetitorIntel")
    assert "cavnarRadarHtml" not in run and "cbtnBusy" not in run
    assert "compLoadingHtml()" in run
    html = _fn("compLoadingHtml")
    assert 'data-orb-state="searching"' in html and "AIV_ORB_SIZE" in html and "aiv-orb" in html


def test_the_loading_block_sits_on_the_page_not_in_a_tinted_card():
    rule = re.search(r"\.in2-pre\.comp-loading\{([^}]*)\}", SRC).group(1)
    assert "background:none" in rule
    assert re.search(r"\.comp-radar \.aiv-orb\{[^}]*width:260px;height:260px", SRC)


def test_a_finished_or_failed_refresh_puts_everything_back_and_stops_the_orb():
    run = _fn("refreshCompetitorIntel")
    reset = run[run.index("function _resetBtn()"):]
    for needed in ("clearInterval(ticker)", "CavnarOrb.destroy", "host.style.display = ''",
                   "hidden[j].style.display = ''", "btn.disabled = false", "btn.textContent = wasText"):
        assert needed in reset, needed
