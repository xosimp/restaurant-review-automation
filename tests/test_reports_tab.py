"""Reports, one tap from anywhere (owner, 9/30/26): "we need to have a spot
called Reports where owners can easily view all reports instead of always
having to go to the home page to click on Report."

A top-level Reports tab beside Home opens the daily report panel on its
list - every night, newest first, with the 3-second status the list API
already carries (dsr.access.summary) - and Night, Week and Period sit
beside it. #dsr/list is the address."""
import os
import re

SRC = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_reports_tab_sits_beside_home_and_opens_the_list():
    tabs = SRC[SRC.index('<div class="tabs" role="tablist"'):SRC.index('<span id="tab-indicator">')]
    assert tabs.index('id="tab-home"') < tabs.index('id="tab-reports"') < tabs.index('id="tab-reviews"')
    assert "dsrOpen('list')" in tabs
    assert "var home=el('tab-reports')||el('tab-home');" in SRC        # the panel lights its own tab


def test_the_list_is_every_night_newest_first_with_older_on_request():
    assert 'data-kind="list" aria-pressed="false">All reports</button>' in SRC
    assert "if(kind==='list')return loadList(null);" in SRC
    body = SRC[SRC.index("function loadList(before){"):SRC.index("document.addEventListener('click',function(e){\n    var t=e.target&&e.target.closest?e.target.closest('[data-dr-older]')")]
    assert "get('/api/dsr?limit=30'" in body and "data-dr-older=" in body
    row = SRC[SRC.index("function listRow(r){"):SRC.index("function loadList(before){")]
    assert 'data-dr="open" data-kind="night"' in row and "r.verdict" in row and "r.net" in row
    assert re.search(r"\(week\|period\|list\)", SRC)                   # #dsr/list routes
