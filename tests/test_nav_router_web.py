"""The web router, the confirm/undo policy's client half and the command
palette (Friction audit 9/25/26, workstream N: items 1, 2, 8, 10, 13, 14,
29, 33, 34, 35, 42).

The <script id="cav-nav"> block runs under node against a small DOM stub,
so the history rules hold for every path rather than for one page a
browser happened to render:

  * one navigation is one Back step; a route taken from Back writes nothing;
  * Back returns to the previous tab (one popstate, the only one);
  * a section, a filter and an item each land where they say;
  * each panel keeps its own scroll;
  * Escape closes the top modal first; a modal returns focus;
  * the reversible tier commits only after its Undo window.

The rest are pinned against the source: what must be true everywhere.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _block(block_id):
    m = re.search(r'<script id="' + block_id + r'">(.*?)</script>', _src(), re.S)
    assert m, block_id + " block is missing"
    return m.group(1)


STUB = r"""
var _log = [];
function El(id, attrs) {
  this.id = id || ''; this.style = {}; this.attrs = attrs || {}; this.hidden = false; this.tagName = 'DIV';
  var cls = {}; this.classList = {
    add: function (c) { cls[c] = 1; }, remove: function (c) { delete cls[c]; },
    contains: function (c) { return !!cls[c]; }, toggle: function (c, on) { if (on === undefined ? !cls[c] : on) cls[c] = 1; else delete cls[c]; }};
  this.focused = 0;
}
El.prototype.getAttribute = function (k) { return this.attrs.hasOwnProperty(k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.hasAttribute = function (k) { return this.attrs.hasOwnProperty(k); };
El.prototype.addEventListener = function (n, f) { (this['_' + n] = this['_' + n] || []).push(f); };
El.prototype.scrollIntoView = function () { _log.push('scroll:' + this.id); };
El.prototype.focus = function () { this.focused++; document.activeElement = this; };
El.prototype.querySelector = function () { return null; };
El.prototype.closest = function () { return null; };
El.prototype.compareDocumentPosition = function () { return 0; };
var els = {}, sections = [];
function add(id, attrs) { var e = new El(id, attrs); els[id] = e; return e; }
var loc = {hash: '', pathname: '/', search: ''};
var hist = {pushes: [], replaces: [],
  pushState: function (s, t, h) { this.pushes.push(h); loc.hash = h; },
  replaceState: function (s, t, h) { this.replaces.push(h); loc.hash = h; }};
var winL = {}, docL = {};
var window = {addEventListener: function (n, f) { (winL[n] = winL[n] || []).push(f); }, location: loc, history: hist,
  pageYOffset: 0, scrollTo: function (x, y) { window.pageYOffset = y; }, getComputedStyle: function (e) { return {display: e.style.display || 'none'}; }};
var history = hist, location = loc;
var document = {addEventListener: function (n, f) { (docL[n] = docL[n] || []).push(f); },
  getElementById: function (id) { return els[id] || null; },
  querySelector: function (sel) {
    if (sel === '.panel.active') { for (var k in els) if (els[k].classList.contains('panel') && els[k].classList.contains('active')) return els[k]; return null; }
    var m = /^\[data-nav="(.*)"\]$/.exec(sel);
    if (m) { for (var i = 0; i < sections.length; i++) if (sections[i].attrs['data-nav'] === m[1]) return sections[i]; return null; }
    return null;
  },
  querySelectorAll: function () { return []; },
  documentElement: {style: {setProperty: function () {}}}, activeElement: null, body: {contains: function () { return true; }}};
var TABS = ['home', 'reviews', 'labor', 'inventory', 'account'];
for (var t = 0; t < TABS.length; t++) { add('tab-' + TABS[t]); var p = add('panel-' + TABS[t]); p.classList.add('panel'); }
els['panel-home'].classList.add('active');
/* The real switchTab's contract: remember the panel left, show the new
   one, write '#<tab>' through cavHistory, put its scroll back. */
function switchTab(n) {
  var was = document.querySelector('.panel.active'), same = was && was.id === 'panel-' + n;
  if (was && !same) cavScrollSave(was.id);
  for (var i = 0; i < TABS.length; i++) els['panel-' + TABS[i]].classList.remove('active');
  els['panel-' + n].classList.add('active');
  _log.push('tab:' + n);
  cavHistory('#' + n);
  if (!same) cavScrollRestore('panel-' + n);
}
function toast(msg, type, action) { _log.push('toast:' + msg); window._toastAction = action; }
function apiJson(r) { return r; }
function setTimeout(fn, ms) { (window._timers = window._timers || []).push({fn: fn, ms: ms}); return window._timers.length; }
function clearTimeout(id) { if (window._timers && window._timers[id - 1]) window._timers[id - 1].fn = null; }
function fire(name) { var fs = (winL[name] || []).concat(docL[name] || []); for (var i = 0; i < fs.length; i++) fs[i]({}); }
function nav(h) { loc.hash = h; fire('popstate'); fire('hashchange'); }
"""


def _run(js_body):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = STUB + _block("cav-nav") + "\n" + js_body
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ── the router ───────────────────────────────────────────────────────────────

def test_parse_reads_heads_rests_and_queries():
    got = _run("""console.log(JSON.stringify([cavNavParse('#reviews?filter=urgent'), cavNavParse('review/412'),
      cavNavParse('ask?q=why%20is%20labor%20high'), cavNavParse('dsr/week/2026-09-21'), cavNavParse('')]));""")
    assert got[0]["head"] == "reviews" and got[0]["query"] == {"filter": "urgent"}
    assert got[1]["head"] == "review" and got[1]["rest"] == ["412"] and got[1]["module"] == "reviews"
    assert got[2]["query"]["q"] == "why is labor high"
    assert got[3]["rest"] == ["week", "2026-09-21"]
    assert got[4] is None


def test_one_navigation_is_one_back_step_and_a_section_keeps_its_address():
    got = _run("""
var sec = new El('lab-sched', {'data-nav': 'labor/schedule'}); sections.push(sec);
cavNav('labor/schedule');
var a = {pushes: hist.pushes.slice(), replaces: hist.replaces.slice(), hash: loc.hash, log: _log.slice()};
cavNav('reviews');
console.log(JSON.stringify([a, hist.pushes, loc.hash]));""")
    first, pushes, hash_ = got
    assert first["pushes"] == ["#labor"], "the tab switch pushes once"
    assert first["replaces"] == ["#labor/schedule"], "the section replaces inside the same step"
    assert first["hash"] == "#labor/schedule" and "scroll:lab-sched" in first["log"]
    assert pushes == ["#labor", "#reviews"] and hash_ == "#reviews"


def test_back_returns_to_the_previous_tab_and_writes_nothing():
    got = _run("""
cavNav('labor'); cavNav('reviews');
var before = hist.pushes.length + hist.replaces.length;
nav('#labor');
console.log(JSON.stringify({writes: hist.pushes.length + hist.replaces.length - before,
  active: document.querySelector('.panel.active').id, tabs: _log.filter(function (x) { return x.indexOf('tab:') === 0; })}));""")
    assert got["active"] == "panel-labor", "Back lands on the tab before, not outside the app"
    assert got["writes"] == 0, "routing Back writes no history"
    assert got["tabs"] == ["tab:labor", "tab:reviews", "tab:labor"], "popstate and hashchange route once"


def test_each_panel_keeps_its_own_scroll():
    got = _run("""
cavNav('labor'); window.pageYOffset = 900; cavScrollSave('panel-labor');
cavNav('reviews'); var onReviews = window.pageYOffset; window.pageYOffset = 300; cavScrollSave('panel-reviews');
nav('#labor'); var backOnLabor = window.pageYOffset;
console.log(JSON.stringify([onReviews, backOnLabor]));""")
    assert got == [0, 900], "a new panel opens at its top; Back puts Labor where it was"


def test_handlers_focus_items_and_unknown_heads_open_their_module():
    got = _run("""
var seen = [];
cavNavRegister('review', function (p) { seen.push('review:' + p.rest[0]); return true; });
cavNavRegister('person', function () { return false; });
cavNav('review/412'); cavNav('person/dana-k'); cavNav('nonsense/1');
console.log(JSON.stringify({seen: seen, tabs: _log.filter(function (x) { return x.indexOf('tab:') === 0; })}));""")
    assert got["seen"] == ["review:412"]
    assert got["tabs"] == ["tab:labor", "tab:home"], "a handler that declines falls back to its module; unknown → Home"


def test_account_sections_have_addresses():
    got = _run("""
window.acctGo = function (s) { _log.push('acct:' + s); cavHistory('#account/' + s, true); return true; };
cavNav('account/notifications');
console.log(JSON.stringify({hash: loc.hash, pushes: hist.pushes, log: _log}));""")
    assert got["hash"] == "#account/notifications" and got["pushes"] == ["#account"]
    assert "acct:notifications" in got["log"]


# ── the reversible tier and the modal helper ─────────────────────────────────

def test_an_undoable_action_commits_only_after_its_window():
    got = _run("""
var commits = 0, restores = 0;
cavUndoable('Chat deleted', function () { commits++; }, function () { restores++; });
var t1 = window._timers[0];
window._toastAction.fn();                      // Undo
if (t1.fn) t1.fn();                            // the window ends
cavUndoable('Chat deleted', function () { commits++; }, function () { restores++; });
window._timers[1].fn();                        // no Undo: the window ends
console.log(JSON.stringify({commits: commits, restores: restores, ms: t1.ms, label: window._toastAction.label}));""")
    assert got == {"commits": 1, "restores": 1, "ms": 7000, "label": "Undo"}


def test_escape_closes_the_top_modal_and_focus_goes_back():
    got = _run("""
var opener = new El('btn'); opener.focus();
var m = add('pw-modal'); m.querySelector = function () { return null; };
fire('DOMContentLoaded');
cModal.open('pw-modal');
var shownAfterOpen = m.style.display;
var esc = (docL.keydown || []);
for (var i = 0; i < esc.length; i++) esc[i]({key: 'Escape', keyCode: 27, preventDefault: function () {}});
console.log(JSON.stringify({open: shownAfterOpen, closed: m.style.display, back: opener.focused, cls: m.classList.contains('cmodal')}));""")
    assert got["open"] == "flex" and got["closed"] == "none"
    assert got["back"] >= 2, "focus returns to what opened it"
    assert got["cls"], "one stacking level (.cmodal → --z-modal)"


# ── against the source ───────────────────────────────────────────────────────

def test_there_is_one_popstate_and_switch_tab_writes_through_the_router():
    src = _src()
    assert src.count("addEventListener('popstate'") == 1, "one router owns Back"
    body = re.search(r"\nfunction switchTab\(n,btn\)\{.*?\n\}", src, re.S).group(0)
    assert "cavHistory('#'+n)" in body and "cavScrollRestore(" in body
    assert "history.replaceState(null,null,'#'+n);\n  fetch" not in body, "a tab switch is a Back step now"
    assert "cavNavRegister('dsr'" in src and "cavNavRegister('recs'" in src


def test_the_script_defining_the_router_runs_before_every_module_script():
    src = _src()
    router = src.index('<script id="cav-nav">')
    for marker in ("window.hbOpen=function", "window.dsrOpen=function", "window.rhOpen=function",
                   "function openNotification"):
        assert src.index(marker) > router, marker


def test_links_from_outside_open_the_item():
    src = _src()
    body = re.search(r"function checkTabParam\(\)\{.*?\n\}", src, re.S).group(0)
    assert "params.get('review')" in body and "params.get('nav')" in body
    # re-audit F1-13 rewrote the body (loc switch, ?tab= dropped); the
    # rule it pins is unchanged: nav, else the review, through cavNav.
    assert "var path=navp||(rv?'review/'" in body and "cavNav(path,{replace:true})" in body


def test_every_take_me_there_uses_the_nav():
    src = _src()
    # the bell: its nav, else the review, else its module; another location's row switches first (H #24)
    assert "var path = n.nav || (n.review_id ? 'review/' + n.review_id" in src, "the bell"
    assert "if (typeof window.cavNav === 'function' && path) { window.cavNav(path); return; }" in src
    assert "window.hbOpen=function(module,nav){\n    if(nav&&window.cavNav){cavNav(nav);return;}" in src
    assert src.count("esc(a.action.nav||'')") == 2, "attention rows and the focus card's also-row"
    # Still open lands on the item (a proposal reopens): the row's own nav rides into hbQueueActs (merged with O1)
    assert "+hbQueueActs(hbWithNav(y))" in src and "if(!o.route&&!o.nav&&y.nav)o.nav=y.nav;" in src
    # Drawn, less the ones a Needs-attention row already carries (density
    # fix #20); the palette's One tap keeps every one.
    assert "renderQuick(hbQuickUnsaid(d.quick_actions||[],d))" in src, "the server's quick actions are drawn"
    assert "window._hbQuick=d.quick_actions||[];" in src


def test_publish_n_replies_opens_the_confirm_card():
    src = _src()
    body = re.search(r"function hbPublish\(btn,count\)\{.*?\n  \}", src, re.S).group(0)
    assert "/api/command/propose" in body and "approve_all_reviews" in body
    assert "/api/reviews/approve-all" not in body, "the first tap posts nothing"


def test_prompt_is_gone_and_reversible_deletes_have_undo():
    src = _src()
    assert not re.search(r"[^\w.]prompt\(['\"]", src), "inline pre-filled fields (cField) replace prompt()"
    for gone in ("confirm('Delete this chat?')", "confirm('Stop tracking this competitor?')",
                 "confirm('Remove this webhook?')"):
        assert gone not in src
    assert src.count("cavUndoable(") >= 4
    # Security and account-destructive steps keep their confirm().
    assert "confirm('Disable two-factor authentication?" in src
    assert "confirm('Regenerate backup codes?" in src


def test_full_page_reloads_after_the_named_actions_are_gone():
    src = _src()
    pub = re.search(r"window.publishAllReplies=function\(btn\)\{.*?\n  \};", src, re.S).group(0)
    assert "location.reload" not in pub and "rvReloadInbox" in pub
    assert "toast('Reply retracted','success');if(window.rvReloadInbox)rvReloadInbox()" in src
    assert "toast('Profile saved','success');cavRefreshFromServer(['acct-restaurant']" in src
    assert "?tab=competitor';" not in src, "a competitor refresh re-reads the panel in place"
    # new reviews land in the inbox in place (H #20) — the banner no longer reloads
    assert 'onclick="rvShowNew()"' in src and "location.reload" not in src[src.index("window.rvShowNew=function"):][:600]


def test_panels_stop_refetching_on_every_visit():
    src = _src()
    body = re.search(r"\nfunction switchTab\(n,btn\)\{.*?\n\}", src, re.S).group(0)
    assert "cavStale('intel-recs')" in body
    assert "loadRecentTopics();startMetricsPoller()" not in body, "topics come from the poller's first tick"
    assert "cavBench.fillPanel(_swPanel)" in body
    assert "now-_openedAt<5*60*1000" in src


def test_tabs_stick_and_sections_scroll_under_them():
    src = _src()
    assert re.search(r"\.tabs\{[^}]*position:sticky;top:56px", src)
    assert src.count('data-nav="account/') == 11, "every Account rail section has an address"
    assert '<a href="#account/notifications" data-go="notifications">' in src


def test_the_palette_opens_on_command_k_and_enter_never_confirms():
    js = _block("cav-palette-js")
    assert "(e.metaKey || e.ctrlKey) && (k === 'k' || k === 'K')" in js
    assert "e.keyCode === 13 && !(e.metaKey || e.ctrlKey)) { e.preventDefault(); run(on); }" in js
    assert "[data-prop-confirm]" in js and "b.click()" in js, "only ⌘Enter or a click confirms a card"
    assert "/api/command/registry" in js and "/api/command/search" in js and "/api/command/propose" in js
    assert "cavPropCard(d.proposal, card" in js, "the same confirm card Ask renders"
    assert "Ask Cavnar: " in js, "the last row is always Ask"
