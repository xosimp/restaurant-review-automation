"""A tab left open across a deploy is offered a reload (audit 10/26/26,
A06-13 / roadmap P3).

The page is rendered with its build (config.build_id, as `cavnar_build`);
the shared fetch wrapper (templates/_csrf_fetch.html) reads X-Cavnar-Build
off every same-origin JSON answer and, the first time it names another
build, shows one "Cavnar AI was updated." bar with a Reload button. These
run the real wrapper — rendered by Jinja exactly as a page includes it —
under node, with a stubbed DOM and stubbed responses.
"""
import json
import os
import shutil
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")

PAGE_BUILD = "abc123def456"
FULL_SAME = PAGE_BUILD + "7890abcdef0123456789abcd"     # the same commit, all 40 characters
OTHER = "fff999eee888"


def _wrapper_js(build):
    """The wrapper's script as a page renders it for `build` (None: a
    page whose app sets no build — a test app, or a local run)."""
    env = Environment(loader=FileSystemLoader(TEMPLATES))
    ctx = {} if build is None else {"cavnar_build": build}
    html = env.get_template("_csrf_fetch.html").render(**ctx)
    start, end = html.index("<script>") + len("<script>"), html.rindex("</script>")
    return html[start:end]


_HARNESS = r"""
var vm = require('vm');
function El(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {};
  this.parentNode = null; this.textContent = ''; this.className = ''; this.id = ''; this.type = ''; }
El.prototype.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1);
  c.parentNode = null; return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return this.attrs.hasOwnProperty(k) ? this.attrs[k] : null; };
function walk(el, fn) { for (var i = 0; i < el.children.length; i++) { fn(el.children[i]); walk(el.children[i], fn); } }
function Resp(status, headers, body) {
  var h = {}; Object.keys(headers || {}).forEach(function (k) { h[k.toLowerCase()] = headers[k]; });
  this.status = status; this.body = body;
  this.headers = {get: function (k) { k = String(k).toLowerCase(); return h.hasOwnProperty(k) ? h[k] : null; }};
}
Resp.prototype.clone = function () { return this; };
Resp.prototype.json = function () { return Promise.resolve(this.body); };

function makePage(js, opts) {
  opts = opts || {};
  var head = new El('head'), body = new El('body');
  var doc = {
    cookie: 'csrf_js=tok123', head: head, body: body,
    createElement: function (t) { return new El(t); },
    getElementById: function (id) {
      var hit = null; [head, body].forEach(function (root) { walk(root, function (e) { if (!hit && e.id === id) hit = e; }); });
      return hit;
    },
    querySelector: function () { return null; }
  };
  if (opts.sessionEnded) { var se = new El('div'); se.id = 'session-ended'; body.appendChild(se); }
  var page = {reloads: 0, calls: [], queue: []};
  var win = {location: {origin: 'https://dashboard.cavnar.ai', reload: function () { page.reloads++; }}};
  win.fetch = function (input, init) {
    page.calls.push({input: input, init: init});
    return Promise.resolve(page.queue.shift());
  };
  vm.runInNewContext(js, {window: win, document: doc, setTimeout: setTimeout, Promise: Promise});
  page.win = win; page.doc = doc;
  page.send = function (resp, url, init) { page.queue.push(resp); return win.fetch(url || '/api/x', init); };
  page.bars = function () { var n = []; walk(body, function (e) { if (e.id === 'cav-updated') n.push(e); }); return n; };
  page.describe = function () {
    var bars = page.bars();
    if (!bars.length) return {bars: 0, changed: win.cavBuildChanged()};
    var b = bars[0], kids = b.children;
    return {bars: bars.length, changed: win.cavBuildChanged(), role: b.getAttribute('role'),
            text: kids[0].textContent, buttons: kids.slice(1).map(function (k) {
              return {tag: k.tagName, type: k.type, cls: k.className, text: k.textContent}; }),
            css: !!doc.getElementById('cav-updated-css')};
  };
  page.click = function (label) {
    var b = page.bars()[0];
    if (!b) return;
    b.children.forEach(function (k) { if (k.textContent === label) k.onclick(); });
  };
  return page;
}
function json(build) { var h = {'Content-Type': 'application/json'}; if (build !== undefined) h['X-Cavnar-Build'] = build; return h; }

var out = {};
var P = Promise.resolve();
function step(name, fn) { P = P.then(fn).then(function (v) { out[name] = v; }); }

step('same_build', function () {
  var p = makePage(JS.page);
  return p.send(new Resp(200, json(PAGE_BUILD), {ok: true}))
    .then(function () { return p.send(new Resp(200, json(FULL_SAME), {ok: true})); })
    .then(function () { return p.send(new Resp(200, json('unknown'), {ok: true})); })
    .then(function () { return p.send(new Resp(200, json(undefined), {ok: true})); })
    .then(function () { return p.send(new Resp(200, {'Content-Type': 'text/html', 'X-Cavnar-Build': OTHER}, '')); })
    .then(function () { return p.send(new Resp(200, json(OTHER), {}), 'https://elsewhere.example/api'); })
    .then(function () { return p.describe(); });
});
step('new_build', function () {
  var p = makePage(JS.page), first;
  return p.send(new Resp(200, json(OTHER.toUpperCase()), {ok: true, n: 7}))
    .then(function (r) { first = r; return r.json(); })
    .then(function (d) {
      var after = p.describe();
      after.passed_through = first.status === 200 && d.n === 7;
      return p.send(new Resp(500, json(OTHER), {ok: false})).then(function () {
        after.after_second = p.bars().length;
        p.click('Reload');
        after.reloads = p.reloads;
        return after;
      });
    });
});
step('not_now', function () {
  var p = makePage(JS.page);
  return p.send(new Resp(200, json(OTHER), {}))
    .then(function () { p.click('Not now'); return p.send(new Resp(200, json('0123456789ab'), {})); })
    .then(function () { return {bars: p.bars().length, changed: p.win.cavBuildChanged(), reloads: p.reloads}; });
});
step('local_page', function () {
  var p = makePage(JS.local);
  return p.send(new Resp(200, json(OTHER), {})).then(function () { return p.describe(); });
});
step('unset_page', function () {
  var p = makePage(JS.unset);
  return p.send(new Resp(200, json(OTHER), {})).then(function () { return p.describe(); });
});
step('session_ended', function () {
  var p = makePage(JS.page, {sessionEnded: true});
  return p.send(new Resp(200, json(OTHER), {})).then(function () { return p.describe(); });
});
step('csrf_kept', function () {
  var p = makePage(JS.page);
  return p.send(new Resp(200, json(PAGE_BUILD), {}), '/api/save', {method: 'POST', headers: {}})
    .then(function () { return p.calls[0].init.headers['X-CSRF']; });
});
step('in_flight_retry', function () {
  var p = makePage(JS.page);
  p.queue.push(new Resp(429, json(OTHER), {in_flight: true}));
  p.queue.push(new Resp(200, json(OTHER), {ok: true}));
  return p.win.fetch('/api/x').then(function (r) { return {status: r.status, calls: p.calls.length, bars: p.bars().length}; });
});
P.then(function () { process.stdout.write(JSON.stringify(out)); },
       function (e) { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        if os.getenv("CI"):
            pytest.fail("node is not on PATH")
        pytest.skip("node is not installed")
    consts = {"JS": {"page": _wrapper_js(PAGE_BUILD), "local": _wrapper_js(""), "unset": _wrapper_js(None)},
              "PAGE_BUILD": PAGE_BUILD, "FULL_SAME": FULL_SAME, "OTHER": OTHER}
    program = "".join("var %s = %s;\n" % (k, json.dumps(v)) for k, v in consts.items()) + _HARNESS
    proc = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout)


def test_the_page_carries_its_build():
    assert '_normBuild("abc123def456")' in _wrapper_js(PAGE_BUILD)
    assert '_normBuild("")' in _wrapper_js(None)


def test_the_same_build_unknown_html_and_external_answers_show_nothing(results):
    assert results["same_build"] == {"bars": 0, "changed": False}


def test_a_new_build_shows_one_bar_with_a_reload(results):
    r = results["new_build"]
    assert r["bars"] == 1 and r["changed"] is True and r["css"] is True
    assert r["role"] == "status"
    assert r["text"] == "Cavnar AI was updated."
    assert r["buttons"] == [
        {"tag": "BUTTON", "type": "button", "cls": "cbtn cbtn-primary cbtn-sm", "text": "Reload"},
        {"tag": "BUTTON", "type": "button", "cls": "cbtn cbtn-text cbtn-sm", "text": "Not now"},
    ]
    assert r["passed_through"] is True, "the caller still gets the answer"
    assert r["after_second"] == 1, "one bar, however many answers name the new build"
    assert r["reloads"] == 1


def test_not_now_dismisses_it_for_this_page(results):
    assert results["not_now"] == {"bars": 0, "changed": True, "reloads": 0}


def test_a_page_without_a_build_never_shows_it(results):
    assert results["local_page"] == {"bars": 0, "changed": False}
    assert results["unset_page"] == {"bars": 0, "changed": False}


def test_an_ended_session_is_not_covered(results):
    assert results["session_ended"] == {"bars": 0, "changed": False}


def test_the_wrapper_still_does_its_other_jobs(results):
    assert results["csrf_kept"] == "tok123"
    assert results["in_flight_retry"] == {"status": 200, "calls": 2, "bars": 1}


def test_build_id_is_the_deployed_commit(monkeypatch):
    import config
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert config.build_id() == ""
    monkeypatch.setenv("GIT_COMMIT", " 0123456789abcdef0123 ")
    assert config.build_id() == "0123456789ab"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", FULL_SAME)
    assert config.build_id() == PAGE_BUILD
