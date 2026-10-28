"""Admin audit #26 (10/27/26), fix round 2, stream FE — the console page
(templates/admin.html, templates/client_settings.html) against the shared
contract the backend stream builds (PLAN2):

- the Operations Jobs and Push sparklines draw the server's own daily series
  (jobs().failures_daily, notifications().push_daily — Central days), the AI
  one says it is on UTC days, and a series the server doesn't send is said,
  never drawn as seven zeros (O6/P-11);
- Email "Undeliverable" / a type and Push "Failed" read the light filtered
  row routes over the whole log, not the newest 200 (P-11);
- data sources carry a stale state and filter, and the fleet list a labelled
  Data column (O9); the staff-note Undo posts its parts (W15-h); the
  Deployment card draws the release verdict (O10); the RPOWER store picker
  shows whenever stores come back (F12);
- one shared read queue, two at a time, that navigation empties (P-05);
- one helper each where two drifted (C5); the page's labels and visual
  vocabulary (C10, V09, V19, V23, V30); the legacy settings page's test sends
  confirm and say who receives them (F14).

Source rules read from the template; behaviour run under node (the page's
own functions with the DOM and the network stubbed).
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


PAGE = _read("admin.html")
CSS = PAGE[PAGE.index("<style>") + len("<style>"):PAGE.index("</style>")]


def _fn(name, src=PAGE):
    """One top-level function's source, to the next top-level declaration or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet |\nvar |\n\$\(", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))] + "\n"


def _decl(name, src=PAGE):
    """A top-level `const|let|var NAME = …` statement."""
    m = re.search(r"\n((?:const|let|var) " + re.escape(name) + r"\b.*?;)[ \t]*(?://[^\n]*)?\n", src, re.S)
    assert m, f"{name} is gone"
    return m.group(1) + "\n"


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", TZ="America/Chicago")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# A DOM good enough for the page's helpers: $() finds stub elements by id,
# toast() records what was said.
STUBS = r"""
var _els = {}, _toasts = [];
function El(id){ this.id = id; this.innerHTML = ''; this.textContent = ''; this.title = ''; this.disabled = false;
  this.style = {}; this.dataset = {}; this.hidden = false; this.value = ''; this.className = ''; this.children = [];
  this.classList = {add: function(){}, remove: function(){}, toggle: function(){}, contains: function(){ return false; }}; }
El.prototype.querySelector = function(){ return null; };
El.prototype.querySelectorAll = function(){ return []; };
El.prototype.appendChild = function(c){ this.children.push(c); };
El.prototype.setAttribute = function(k, v){ this['@' + k] = String(v); };
El.prototype.removeAttribute = function(k){ delete this['@' + k]; };
function $(id){ return _els[id] || null; }
function mk(id){ _els[id] = new El(id); return _els[id]; }
function toast(msg, tone){ _toasts.push([msg, tone || '']); }
var document = {body: {classList: {add: function(){}}}, hidden: false, createElement: function(){ return new El(''); },
  querySelector: function(){ return null; }, querySelectorAll: function(){ return []; }};
var location = {origin: 'http://admin.test', href: '', hash: '#operations'};
var window = {};
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const jsq = (v) => esc(String(v == null ? '' : v));
const fmtN = (n) => (n == null ? '—' : Number(n).toLocaleString());
function ico(n){ return '<i:' + n + '>'; }
function openModal(){} function closeModal(){} function route(){ _routed++; } var _routed = 0;
function confirm(){ return true; }
"""


def _js(fns=(), decls=(), src=PAGE):
    return STUBS + "".join(_decl(n, src) for n in decls) + "".join(_fn(n, src) for n in fns) + "\n"


# ── O6 / P-11: the Operations trends are the server's daily series ─────────

def test_the_jobs_and_push_trends_draw_the_servers_daily_series_and_say_when_it_is_missing():
    js = _js(("daySeries", "spark", "subCard", "okRes"), ("ST_WORD", "ST_COLOR")) + r"""
    var _opsTab = 'jobs';
    var rows = [];
    for(var i = 0; i < 9; i++) rows.push({day: '2026-10-' + (10 + i), n: i, failed: 1});
    var s = daySeries(rows, 'n');
    var none = daySeries(undefined, 'n');
    var card = subCard('push', 'bell', 'Push', 'good', '4', 'delivered today', 'ok', s, 'var(--green-t)', null, 'Push deliveries a day · 7 days, Central days');
    var miss = subCard('jobs', 'clock', 'Jobs', 'good', '3/3', 'healthy', 'ok', none, 'var(--green-t)', null, 'Failed job runs a day');
    var nada = subCard('api', 'gauge', 'API', 'good', '80', 'ms', 'ok', null);
    console.log(JSON.stringify({s: s, none: none, card: card, miss: miss, nada: nada}));
    """
    out = _node(js)
    assert out["s"] == [2, 3, 4, 5, 6, 7, 8]                 # the newest seven, oldest first, as sent
    assert out["none"] is False                               # not sent is not zeros
    assert 'title="Push deliveries a day · 7 days, Central days"' in out["card"] and "spk" in out["card"]
    assert "7-day trend not sent by the server" in out["miss"] and "spk" not in out["miss"]
    assert "not sent" not in out["nada"]                      # a card with no trend by design stays blank


def test_operations_reads_the_series_from_the_payloads_and_labels_the_ai_trend_utc():
    ops = _fn("operations")
    assert "daySeries(o.jb.failures_daily, 'n')" in ops and "daySeries(o.nt.push_daily, 'n')" in ops
    assert "daySeries(o.em.daily, 'n')" in ops
    assert "perDay" not in PAGE and "function dayKeys(" not in PAGE  # the browser's own bucketing is gone
    assert "UTC days" in ops.split("subCard('ai'")[1].split("\n")[0]
    for card in ("subCard('jobs'", "subCard('email'", "subCard('push'"):
        assert "Central days" in ops.split(card)[1].split("\n")[0], card


# ── P-11: Undeliverable, a type and Failed read the whole log ───────────────

def test_a_filter_reads_the_light_row_route_newest_answer_only_and_fails_honestly():
    js = _js(("filteredRows", "readErr", "pulse"), ("STALE", "_rowSeq")) + r"""
    var answers = {}, calls = [];
    function api(path){ calls.push(path); return new Promise(function(res){ var a = answers[path]; setTimeout(function(){ res(a[1]); }, a[0]); }); }
    var draw = function(rows){ return '<table ' + rows.length + '>'; }, pick = function(x){ return x.rows; };
    mk('em-rows'); mk('em-n');
    var rows = []; for(var i = 0; i < 200; i++) rows.push({id: i});
    answers['/slow'] = [40, {ok: true, rows: [{id: 'old'}], limit: 200}];
    answers['/fast'] = [5, {ok: true, rows: rows, limit: 200}];
    answers['/bad'] = [5, {ok: false, _status: 404, error: 'Not found'}];
    (async function(){
      var p1 = filteredRows('em', 'em-rows', 'em-n', '/slow', 'the undeliverable emails', pick, draw);
      var reading = _els['em-rows'].innerHTML;
      var p2 = filteredRows('em', 'em-rows', 'em-n', '/fast', 'the undeliverable emails', pick, draw);
      await Promise.all([p1, p2]);
      var drawn = _els['em-rows'].innerHTML, n = _els['em-n'].textContent;
      await filteredRows('em', 'em-rows', 'em-n', '/bad', 'the undeliverable emails', pick, draw);
      console.log(JSON.stringify({reading: reading, drawn: drawn, n: n, bad: _els['em-rows'].innerHTML, calls: calls}));
    })();
    """
    out = _node(js)
    assert "dr-pulse" in out["reading"] and "from the whole log" in out["reading"]   # the house pulse while it reads
    assert out["drawn"].startswith("<table 200>")                                    # the older, slower answer is dropped
    assert "The newest 200 that match" in out["drawn"] and out["n"] == "200+"
    assert "Couldn't read the undeliverable emails — Not found" in out["bad"] and "<table" not in out["bad"]


def test_email_and_push_filters_call_the_contract_routes():
    em, push = _fn("emRowsLoad"), _fn("pushRowsLoad")
    assert "'/admin/api/emails/rows?' + (f === 'failed' ? 'status=undeliverable' : 'type=' + encodeURIComponent(f)) + '&limit=200'" in em
    assert "'/admin/api/notifications/pushes?status=failed&limit=200'" in push and "x.pushes" in push
    ops_em, ops_push = _fn("opsEmail"), _fn("opsPush")
    # the newest-200 sample is never filtered in the browser any more
    assert "d.rows.filter(" not in ops_em and "d.pushes.filter(" not in ops_push
    assert "if(_ef !== 'all') emRowsLoad();" in ops_em and "if(_nf === 'failed') pushRowsLoad();" in ops_push
    assert "(d.by_type || []).map(x => x.email_type)" in ops_em   # every type sent in 7 days is offered
