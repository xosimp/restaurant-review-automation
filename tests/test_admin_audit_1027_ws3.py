"""Admin audit #26 (10/27/26), fix stream WS-3 — the console's JavaScript
behaviour (templates/admin.html):

- a write whose answer never came back is said and gives its button back
  (W4/C2); a page's reads are aborted when the admin moves on and run two at
  a time at most (P-05);
- one platform verdict: the Overview ring, the rail's scheduler pill and
  Operations' system cards all draw GET /admin/api/ops/state, with no
  thresholds of the page's own (W2/O1/V04);
- one humanising duration ("28d", "3h", "12m" — never "41,020m") (L1);
- a slow write never redraws into another client's page (W3); polls go
  through pollJob and survive navigation (C3/W10/O11); modal forms send once
  (C11/W15-f); a slower search never overwrites a newer one (W14); jobs
  started from Needs-you and the POS modals are followed (W5); Mark signed
  says what was sent (W6); Operations refreshes after a cap (W7); Lift hold
  asks for its note first (W1); Cmd-K says when search failed (V29); a rate
  over an empty base is "—" (L2, K-12).

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


@pytest.fixture(scope="module")
def page():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    """One top-level function's source, to the next top-level declaration or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet |\nvar |\n\$\(", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def _decl(src, name):
    """A top-level `const|let|var NAME = …` line."""
    m = re.search(r"\n((?:const|let|var) " + re.escape(name) + r"\b[^\n]*)", src)
    assert m, f"{name} is gone from admin.html"
    return m.group(1)


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
  this.style = {}; this.dataset = {}; this.hidden = false; this.value = ''; this.className = '';
  this.classList = {add: function(){}, remove: function(){}, toggle: function(){}, contains: function(){ return false; }}; }
El.prototype.querySelector = function(){ return null; };
El.prototype.querySelectorAll = function(){ return []; };
El.prototype.scrollIntoView = function(){};
function $(id){ return _els[id] || null; }
function mk(id){ _els[id] = new El(id); return _els[id]; }
function toast(msg, tone){ _toasts.push([msg, tone || '']); }
var document = {body: {classList: {add: function(){}}}, hidden: false,
  querySelector: function(){ return null; }, querySelectorAll: function(){ return []; }};
var location = {origin: 'http://admin.test', href: '', hash: '#overview'};
var window = {};
function ico(n){ return '<i:' + n + '>'; }
function openModal(){} function closeModal(){} function route(){ _routed++; } var _routed = 0;
function confirm(){ return true; }
"""


def _page_js(page, fns=(), decls=()):
    return STUBS + "\n".join(_decl(page, n) for n in decls) + "\n" + "".join(_fn(page, n) for n in fns) + "\n"


# ── W4/C2: a write whose answer never came back ─────────────────────────────

def test_a_write_that_gets_no_answer_says_so_and_a_read_still_throws(page):
    js = _page_js(page, ("isWrite", "api", "post", "okRes", "errText", "say", "opsWrite"),
                  ("STALE", "_seq", "NO_ANSWER", "_readCtl", "_reauthP")) + r"""
    function reauth(){ return Promise.resolve(false); }
    var fetch = function(){ return Promise.reject(new TypeError('Failed to fetch')); };
    (async function(){
      var w = await post('/admin/api/client/5/delete', {confirm_name: 'X'});
      var shown = say(w, 'Deleted');
      var o = await opsWrite('/admin/api/ai/reset-breaker', {}, null, null, 'Reset');
      var readThrew = false; try{ await api('/admin/api/overview'); }catch(e){ readThrew = e.message; }
      var up = await api('/admin/staff-notes/5', {method: 'POST', body: 'x'});
      console.log(JSON.stringify({w: w, shown: shown, toasts: _toasts, o: o, readThrew: readThrew, up: up}));
    })();
    """
    out = _node(js)
    w = out["w"]
    assert w["ok"] is False and w["_status"] == 0 and w["network"] is True
    assert "outcome is unknown" in w["error"]
    assert out["shown"] is False and out["toasts"][0] == [w["error"], "bad"]
    assert out["o"]["ok"] is False and out["toasts"][1][1] == "bad"           # opsWrite says it once, in red
    assert out["readThrew"] == "Failed to fetch"                              # a read is not dressed up as a write
    assert out["up"]["ok"] is False and out["up"]["_status"] == 0            # uploads (FormData POSTs) too


def test_a_read_the_admin_left_is_dropped_and_a_page_read_carries_the_abort_signal(page):
    js = _page_js(page, ("isWrite", "api"), ("STALE", "_seq", "NO_ANSWER", "_readCtl", "_reauthP")) + r"""
    function reauth(){ return Promise.resolve(false); }
    var seen = [];
    var fetch = function(path, o){ seen.push([path, !!o.signal]);
      return new Promise(function(res, rej){
        if(o.signal) o.signal.addEventListener('abort', function(){ rej(new Error('The operation was aborted')); });
        if(path.indexOf('slow') < 0) res({status: 200, headers: {get: function(){ return null; }}, json: function(){ return Promise.resolve({ok: true}); }});
      }); };
    (async function(){
      _readCtl = new AbortController();
      var p = api('/admin/api/slow');
      var poll = await api('/admin/api/admin-jobs/1', {method: 'GET'});
      // The admin moves on: route() aborts the page's reads and moves _seq.
      _readCtl.abort(); _readCtl = new AbortController(); _seq++;
      var got; try{ await p; got = 'drawn'; }catch(e){ got = e.message; }
      console.log(JSON.stringify({seen: seen, got: got, poll: poll.ok}));
    })();
    """
    out = _node(js)
    assert out["seen"][0] == ["/admin/api/slow", True]           # a page read can be cancelled
    assert out["seen"][1] == ["/admin/api/admin-jobs/1", False]  # a poll (a method) never is
    assert out["got"] == "__stale__" and out["poll"] is True
    r = _fn(page, "route")
    assert "_readCtl.abort()" in r and "new AbortController()" in r
    assert r.index("_readCtl.abort()") < r.index("++_seq")


def test_a_page_never_has_more_than_two_reads_in_flight(page):
    assert re.search(r"\nvar API_ALL_MAX = 2;", page)
    js = _page_js(page, ("apiAll",), ("STALE", "API_ALL_MAX")) + r"""
    var now = 0, peak = 0;
    function api(p){ now++; peak = Math.max(peak, now);
      return new Promise(function(res){ setTimeout(function(){ now--; res({ok: true, p: p}); }, 5); }); }
    (async function(){
      var r = await apiAll(['a', 'b', 'c', 'd', 'e', 'f', 'g'], 4);
      console.log(JSON.stringify({peak: peak, got: r.map(function(x){ return x.p; })}));
    })();
    """
    out = _node(js)
    assert out["peak"] == 2 and out["got"] == list("abcdefg")
    # The recommendations page used to fan out four reads with Promise.all.
    rec = _fn(page, "recommendations")
    assert "Promise.all" not in rec and "apiAll([" in rec and "API_ALL_MAX" in rec


def test_a_failed_pool_task_says_its_own_reason(page):
    js = _page_js(page, ("okRes", "errText", "say", "jobSaid")) + r"""
    jobSaid({ok: false, state: 'failed', result: {error: 'Toast refused the credentials'}, status: 'error', _status: 200}, 'Sync Toast');
    jobSaid({ok: true, state: 'ok', result: {}, status: 'done', _status: 200}, 'Sync Toast');
    console.log(JSON.stringify(_toasts));
    """
    out = _node(js)
    assert out[0] == ["Toast refused the credentials", "bad"]
    assert out[1] == ["Sync Toast — finished", "good"]
