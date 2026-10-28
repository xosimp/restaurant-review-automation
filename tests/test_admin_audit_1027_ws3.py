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
    m = re.search(r"\n((?:const|let|var) " + re.escape(name) + r"\b.*?;)[ \t]*(?://[^\n]*)?\n", src, re.S)
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
El.prototype.setAttribute = function(k, v){ this['@' + k] = String(v); };
El.prototype.removeAttribute = function(k){ delete this['@' + k]; };
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
    # api() takes its turn in the page's read queue since round 2 (P-05).
    js = _page_js(page, ("isWrite", "readDone", "readNow", "readTurn", "readPump", "api", "post", "okRes", "errText", "say", "opsWrite"),
                  ("STALE", "_seq", "NO_ANSWER", "_readCtl", "_reauthP", "READ_MAX")) + r"""
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
    js = _page_js(page, ("isWrite", "readDone", "readNow", "readTurn", "readPump", "api"),
                  ("STALE", "_seq", "NO_ANSWER", "_readCtl", "_reauthP", "READ_MAX")) + r"""
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


# ── L1: one humanising duration ─────────────────────────────────────────────

def test_one_duration_formatter_never_prints_tens_of_thousands_of_minutes(page):
    js = _page_js(page, ("parseTs", "durMin", "ago", "agoWord", "minsAgo"), ("fmtN",)) + r"""
    var iso = function(min){ return new Date(Date.now() - min * 60000).toISOString(); };
    console.log(JSON.stringify({
      d: [durMin(null), durMin(0.3), durMin(12), durMin(59.6), durMin(180), durMin(41020), durMin(100000)],
      m: [minsAgo(null), minsAgo(0.2), minsAgo(41019), minsAgo(14)],
      a: [ago(iso(0.1)), ago(iso(3 * 60)), ago(iso(41020)), agoWord(iso(41020))]}));
    """
    out = _node(js)
    assert out["d"] == ["—", "<1m", "12m", "1h", "3h", "28d", "2mo"]
    assert out["m"] == ["—", "just now", "28d ago", "14m ago"]
    assert out["a"] == ["now", "3h", "28d", "28d ago"]


def test_the_heartbeat_lease_and_bounds_read_through_the_formatter(page):
    eng = _fn(page, "engSched")
    assert "durMin(hb)" in eng and "fmtN(Math.round(hb)) + 'm'" not in eng
    assert "durMin(lease.age_seconds / 60)" in eng
    jobs = _fn(page, "opsJobs")
    assert "durMin(h.running_minutes)" in jobs and "durMin(h.stale_after_minutes)" in jobs
    assert "durMin(h.loop_completed_age_minutes)" in jobs
    assert "minutes, past its" not in jobs
    assert "durMin(j.stuck_after_minutes)" in _fn(page, "jobRow")
    assert "fmtN(Math.round(m)) + 'm ago'" not in page


# ── W2/O1/V04: one platform verdict, from the server ────────────────────────

SYSTEMS = ("scheduler", "jobs", "api", "email", "sms", "push", "ai", "integrations")


def _st(**states):
    return {"ok": True, "worst": "ok", "generated_at": "2026-10-27T15:00:00Z", "_status": 200,
            "systems": {n: {"state": states.get(n, "ok"), "reason": n + " reason", "since": None} for n in SYSTEMS}}


VERDICT_FNS = ("okRes", "errText", "parseTs", "mdy", "fmtT", "fmtDT", "durMin", "ago", "agoWord", "esc_", "sysLabel",
               "systemsOf", "sysOf", "railHealth", "platformSystems", "healthRing", "healthCard", "opsStateKeep",
               "opsStateCached", "opsStateRead", "pulse")


def _verdict_js(page):
    fns = tuple(n for n in VERDICT_FNS if n != "esc_")
    return _page_js(page, fns, ("esc", "fmtN", "SYS_COLOR", "SYS_TEXT", "OPS_SYSTEMS", "OPS_STATE_TTL", "HEART",
                                "RING_TONE"))


def test_the_systems_are_the_servers_and_a_failed_or_missing_reading_is_unknown(page):
    st = _st(ai="bad")
    st["systems"]["queue"] = {"state": "warn", "reason": "Generations waiting", "since": None}   # a system WS-1 may add
    del st["systems"]["integrations"]
    js = _verdict_js(page) + "var P = " + json.dumps({"st": st, "failed": {"ok": False, "_status": 500, "error": "locked"}}) + r""";
    var pick = function(l){ return l.map(function(s){ return [s.key, s.state, s.reason]; }); };
    console.log(JSON.stringify({reading: pick(systemsOf(undefined)), failed: pick(systemsOf(P.failed)), st: pick(systemsOf(P.st))}));
    """
    out = _node(js)
    assert [r[0] for r in out["reading"]] == list(SYSTEMS[:2]) + ["api", "ai", "push", "email", "sms", "integrations"]
    assert all(r[1] == "unknown" and r[2] == "reading…" for r in out["reading"])
    assert all(r[1] == "unknown" and "locked" in r[2] for r in out["failed"])
    got = {r[0]: r for r in out["st"]}
    assert got["ai"][1] == "bad" and got["scheduler"][1] == "ok"
    assert got["integrations"][1:] == ["unknown", "not reported by the server"]
    assert got["queue"][1:] == ["warn", "Generations waiting"]                  # tolerated, listed under its key


def test_overview_never_says_all_systems_healthy_through_an_ai_outage(page):
    js = _verdict_js(page) + "var P = " + json.dumps({"outage": _st(ai="bad"), "good": _st(), "failed": {"ok": False, "_status": 500, "error": "locked"}}) + r""";
    var said = function(st){ var h = healthCard(st); return h.slice(h.indexOf('line-height:1.3">') + 17).split('</span>')[0]; };
    console.log(JSON.stringify([said(P.outage), said(P.good), said(undefined), said(P.failed), platformSystems(P.outage).length]));
    """
    out = _node(js)
    assert out[0] == "AI failing"
    assert out[1] == "All systems healthy"
    assert out[2] == "Reading the platform state"               # nothing is healthy before the server answers
    assert out[3] == "Platform state unknown"
    assert out[4] == 8


def test_the_ring_reads_with_the_pulse_bar_and_its_colours_are_tokens(page):
    # Lead follow-up (admin audit 10/27/26, V13/V31 after the WS-3 + WS-4 merge):
    # the ring waiting on the server shows the house pulse, never "…" text, and
    # its unknown arcs and track are tokens, not white literals.
    js = _verdict_js(page) + "console.log(JSON.stringify([healthCard(undefined)]));"
    pending = _node(js)[0]
    assert '<span class="dr-pulse"><i></i></span>' in pending and "…" not in pending.split("</svg>")[1]
    ring = _fn(page, "healthRing")
    assert 'stroke="var(--fill)"' in ring and "rgba(" not in ring
    assert "rgba(" not in _decl(page, "SYS_COLOR")


def test_the_rail_pill_is_the_servers_scheduler_verdict_in_words(page):
    stale = _st(scheduler="bad")
    stale["systems"]["scheduler"].update(reason="No heartbeat for 41020 minutes — nothing scheduled is running.",
                                         since="__SINCE__")
    js = _verdict_js(page) + "var P = " + json.dumps({"stale": stale, "stalled": _st(scheduler="warn"), "failed": {"ok": False, "_status": 0, "error": "down"}}) + r""";
    P.stale.systems.scheduler.since = new Date(Date.now() - 41020 * 60000).toISOString();
    mk('hb-pill'); var out = [];
    [P.stale, P.stalled, P.failed].forEach(function(st){ railHealth(st); out.push([$('hb-pill').innerHTML, $('hb-pill').title]); });
    console.log(JSON.stringify(out));
    """
    out = _node(js)
    assert 'class="heart bad"' in out[0][0] and "28d ago" in out[0][0] and "41020m" not in out[0][0]
    assert "No heartbeat" in out[0][1]                           # the server's reason on hover
    assert 'class="heart warn"' in out[1][0]
    assert 'class="heart unk"' in out[2][0] and ">unknown<" in out[2][0]
    assert ".heart.warn{" in page


def test_the_platform_state_is_read_once_for_everyone_and_a_failure_is_not_kept(page):
    js = _verdict_js(page) + "var P = " + json.dumps({"good": _st()}) + r""";
    var calls = 0, answer = P.good;
    function api(path, opts){ calls++; return new Promise(function(res){ setTimeout(function(){ res(answer); }, 5); }); }
    (async function(){
      var a = opsStateRead(false), b = opsStateRead(false);           // the rail and the page at the same moment
      await a; await b; var one = calls;
      await opsStateRead(false); var cached = calls;                  // fresh: reused
      await opsStateRead(true); var forced = calls;                   // Refresh: read again
      answer = {ok: false, _status: 500, error: 'locked'};
      await opsStateRead(true); var failed = opsStateCached();
      answer = P.good; await opsStateRead(false); var retried = calls;
      console.log(JSON.stringify({one: one, cached: cached, forced: forced, failedKept: failed === undefined, retried: retried}));
    })();
    """
    out = _node(js)
    assert out == {"one": 1, "cached": 1, "forced": 2, "failedKept": True, "retried": 4}


def test_no_page_threshold_judges_a_system_any_more(page):
    for gone in ("hbm <= 15", "hbm <= 60", "er >= 5", "af >= 10", "m > 15", "hb > 15",
                 "k.server_error_rate>=5?'bad'", "h.loop_stalled){ tone = 'bad'"):
        assert gone not in page, gone
    assert "'/admin/api/ops/state'" in _fn(page, "operations") and "opsStateKeep(r[0])" in _fn(page, "operations")
    ov = _fn(page, "overview")
    assert "opsStateRead(ovFresh)" in ov and "/admin/api/messaging/health" not in ov
    assert "opsStateRead(" in _fn(page, "badges")
    assert "systemsOf(o.st)" in _fn(page, "opsState")
    eng = _fn(page, "engSched")
    assert "S.scheduler" in eng and "SCHED_TONE" in eng
    assert "['warn', 'Loop stalled']" in _fn(page, "opsJobs")     # the Jobs tab agrees with the server: stalled is amber


# ── W3: a slow write never redraws into another client's page ───────────────

def test_a_panel_reload_for_one_client_is_not_drawn_on_another_clients_page(page):
    js = _page_js(page, ("onClient", "lazy"), ("STALE",)) + r"""
    var console_error = console.error; console.error = function(){};
    mk('cd-intake').innerHTML = 'B page';
    location.hash = '#client/9?tab=data';                    // the admin opened client 9 meanwhile
    lazy('cd-intake', function(){ return 'A intake'; }, 5);
    lazy('off-card', function(){ return 'never'; }, 5);
    setTimeout(function(){
      var other = $('cd-intake').innerHTML;
      location.hash = '#client/5?tab=data';
      lazy('cd-intake', function(){ return 'A intake'; }, 5);
      setTimeout(function(){
        var same = $('cd-intake').innerHTML;
        lazy('cd-intake', function(){ return 'no rid'; });  // a render-time panel (no rid) is drawn as before
        setTimeout(function(){ console.log(JSON.stringify([other, same, $('cd-intake').innerHTML, onClient(5), onClient(50)])); }, 5);
      }, 5);
    }, 5);
    """
    assert _node(js) == ["B page", "A intake", "no rid", True, False]


def test_every_reload_after_a_client_page_write_names_its_client(page):
    for fn, needle in (("intakeReload", "intakeHtml({id:rid, name:name}), rid)"),
                       ("contactAdd", "contactsHtml({id:id, name:name}), id)"),
                       ("contactDelete", "contactsHtml({id:id, name:name}), id)"),
                       ("templateAdd", "templatesHtml({id:id, name:name}), id)"),
                       ("templateDelete", "templatesHtml({id:id, name:name}), id)"),
                       ("testText", "smsHtml({id:id, name:name}), id)"),
                       ("offStep", "offboardHtml({id:id, name:name}), id)"),
                       ("noteAdd", "notesHtml({id:id, name:name}), id)"),
                       ("auditLink", "salesAuditsHtml({id:pageId, name:name}), pageId)")):
        assert needle in _fn(page, fn), fn
    assert "onClient(id) ? $('cm-test')" in _fn(page, "testText")
    assert "!onClient(id)" in _fn(page, "menuExtract")


# ── W14: a slower search answer never overwrites a newer one ────────────────

def test_a_slower_restaurant_search_answer_is_dropped(page):
    js = _page_js(page, ("okRes", "errText", "clientsView"), ("esc", "_clients", "_cvSeq")) + r"""
    function V(){ return $('view'); } mk('view');
    function errBanner(){ return ''; } function pager(){ return ''; } var CUST_COLS = '';
    function custRow(r){ return '<row ' + r.name + '>'; } function brandRow(b){ return '<brand>'; }
    var answers = [[20, 'Sim'], [5, 'Simple']], n = 0;
    function api(){ var a = answers[n++]; return new Promise(function(res){ setTimeout(function(){
      res({ok: true, _status: 200, items: [{name: a[1], brand: a[1]}], counts: {}, page: 1, pages: 1, total: 1}); }, a[0]); }); }
    (async function(){
      await Promise.all([clientsView(), clientsView()]);
      console.log(JSON.stringify($('view').innerHTML.indexOf('<row Simple>') >= 0 && $('view').innerHTML.indexOf('<row Sim>') < 0));
    })();
    """
    assert _node(js) is True
    iss = _fn(page, "issues")
    assert "var mine = ++_isSeq;" in iss and "if(mine !== _isSeq) return;" in iss


# ── V29: Cmd-K says when the search failed ──────────────────────────────────

def test_a_failed_palette_search_says_so_and_needs_you_is_not_listed_twice(page):
    assert "['Needs you','#overview','Page']" not in page
    js = _page_js(page, ("okRes", "errText", "palNote", "palSearch", "palRender"), ("esc", "PAGES", "_palSel", "_qSeq")) + r"""
    mk('q').value = 'zz'; mk('q-res');
    var answers = [];
    function api(){ var a = answers.shift(); return new Promise(function(res){ setTimeout(function(){ res(a[1]); }, a[0]); }); }
    (async function(){
      answers = [[1, {ok: false, _status: 500, error: 'database is locked'}]];
      await palSearch('zz'); var failed = $('q-res').innerHTML;
      $('q').value = 'sim';
      answers = [[20, {ok: true, _status: 200, results: [{type: 'location', id: 1, title: 'Old answer'}]}],
                 [5, {ok: true, _status: 200, results: [{type: 'location', id: 2, title: 'Simple'}]}]];
      await Promise.all([palSearch('sim'), palSearch('sim')]);
      console.log(JSON.stringify({failed: failed, now: $('q-res').innerHTML}));
    })();
    """
    out = _node(js)
    assert "Search couldn&#39;t run — database is locked" in out["failed"] and "No matches." not in out["failed"]
    assert "Simple" in out["now"] and "Old answer" not in out["now"]


# ── C3/W10/O11/W15-j: every poll is pollJob, and its result lands somewhere ──

def test_poll_job_takes_a_longer_bound_and_says_when_it_ran_out(page):
    js = _page_js(page, ("durMin", "pollJob")) + r"""
    var t = Date.now(); Date.now = function(){ return t += 60000; };   // a minute per look
    var _st = setTimeout; setTimeout = function(f){ return _st(f, 0); };
    var looks = 0; function api(){ looks++; return Promise.resolve({status: 'pending'}); }
    (async function(){
      var r = await pollJob('/admin/api/onboarding/provision/j', null, 20 * 60000);
      console.log(JSON.stringify({r: r, looks: looks}));
    })();
    """
    out = _node(js)
    assert out["r"]["ok"] is False and out["r"]["timed_out"] is True
    assert out["r"]["error"] == "Still running after 20m — check back later."
    assert out["looks"] > 15                                           # it kept looking past the old 3 minutes
    for fn in ("bulkApply", "bulkFollow", "posExport", "posExportFollow"):
        body = _fn(page, fn)
        assert "setTimeout" not in body, fn
    assert "pollJob('/admin/api/onboarding/provision/'" in _fn(page, "bulkFollow")
    assert "pollJob('/admin/api/client/' + id + '/pos-export/'" in _fn(page, "posExportFollow")


def test_a_bulk_list_says_its_counts_wherever_the_admin_is_and_a_partial_failure_is_red(page):
    js = _page_js(page, ("okRes", "errText", "durMin", "bulkFollow"), ("esc", "fmtN", "BULK_MAX_MS")) + r"""
    function bulkRows(rows){ return '<rows ' + rows.length + '>'; }
    var answers = [];
    function pollJob(url, onTick, maxMs){ return Promise.resolve(answers.shift()); }
    (async function(){
      var out = [];
      answers = [{ok: false, status: 'error', counts: {created: 2, exists: 1, error: 1}, rows: [{}, {}, {}, {}], error: 'RPOWER refused one store'}];
      mk('bulk-out'); await bulkFollow('j1'); out.push([$('bulk-out').innerHTML, _toasts.pop()]);
      delete _els['bulk-out'];                                         // the admin moved on
      answers = [{ok: true, status: 'done', counts: {created: 3, exists: 0, error: 0}, rows: [{}, {}, {}]}];
      await bulkFollow('j2'); out.push(_toasts.pop());
      answers = [{ok: false, timed_out: true, error: 'Still running'}];
      await bulkFollow('j3'); out.push(_toasts.pop());
      console.log(JSON.stringify(out));
    })();
    """
    out = _node(js)
    assert "<rows 4>" in out[0][0] and out[0][1][1] == "bad"
    assert "2 restaurants created · 1 not created — RPOWER refused one store" == out[0][1][0]
    assert out[1] == ["3 restaurants created — see Customers → Restaurants", "good"]
    assert "Customers → Restaurants" in out[2][0] and out[2][1] == "bad"
    assert "BULK_MAX_MS, " in _fn(page, "bulkFollow") or "BULK_MAX_MS)" in _fn(page, "bulkFollow")


def test_the_pos_archive_downloads_after_navigation_and_asks_for_the_password_once(page):
    js = _page_js(page, ("okRes", "errText", "downloadOnce", "posExportFollow"), ("NO_ANSWER",)) + r"""
    var clicks = 0, reauths = 0, fetches = [];
    document.createElement = function(){ return {click: function(){ clicks++; }, remove: function(){}}; };
    document.body.appendChild = function(){};
    var URL = {createObjectURL: function(){ return 'blob:x'; }};
    function reauth(){ reauths++; return Promise.resolve(true); }
    var fetch = function(url, o){ fetches.push(o.method);
      if(fetches.length === 1) return Promise.resolve({status: 403, ok: false, json: function(){ return Promise.resolve({reauth_required: true}); }});
      return Promise.resolve({status: 200, ok: true, blob: function(){ return Promise.resolve('zip'); }}); };
    function pollJob(){ return Promise.resolve({ok: true, status: 'done', file: 'ejs.zip', bytes: 20480}); }
    (async function(){
      await posExportFollow(5, 'job-1');                       // #posx-5 is gone: the admin left the Access tab
      console.log(JSON.stringify({clicks: clicks, reauths: reauths, fetches: fetches, toasts: _toasts}));
    })();
    """
    out = _node(js)
    assert out["clicks"] == 1 and out["reauths"] == 1 and out["fetches"] == ["POST", "POST"]
    assert out["toasts"] == [["POS archive for #5: Downloaded 20 KB. Send it to the owner and note where in the export step.", "good"]]


# ── W1: Lift hold asks for its note before the password step ────────────────

def test_an_issue_action_that_needs_a_note_asks_for_it_before_sending(page):
    js = _page_js(page, ("okRes", "errText", "say", "issueNote", "issueAction")) + r"""
    var posted = [], prompts = [], answer = null;
    function prompt(q){ prompts.push(q); return answer; }
    function post(path, body){ posted.push([path, JSON.parse(JSON.stringify(body))]); return Promise.resolve({ok: true, _status: 200}); }
    function badges(){} var _clients = null;
    (async function(){
      var pay = JSON.stringify({note: ''});
      answer = null; await issueAction('/admin/api/billing/5/lift-hold', 'Lift hold', "Simple EJ's", pay);     // cancelled
      answer = '   '; await issueAction('/admin/api/billing/5/lift-hold', 'Lift hold', "Simple EJ's", pay);    // blank
      answer = 'Dispute won'; await issueAction('/admin/api/billing/5/lift-hold', 'Lift hold', "Simple EJ's", pay);
      await issueAction('/admin/api/suppressions/reinstate', 'Reinstate', "Simple EJ's", JSON.stringify({email: 'a@b.c'}));
      console.log(JSON.stringify({posted: posted, prompts: prompts.length, toasts: _toasts}));
    })();
    """
    out = _node(js)
    assert out["posted"] == [["/admin/api/billing/5/lift-hold", {"note": "Dispute won"}],
                             ["/admin/api/suppressions/reinstate", {"email": "a@b.c"}]]   # no note asked for here
    assert out["prompts"] == 3
    assert ["A note is required — nothing was changed", "bad"] in out["toasts"]


# ── W5: jobs started from Needs-you and the POS modals are followed ─────────

def test_a_job_started_from_needs_you_is_followed_and_its_failure_said(page):
    js = _page_js(page, ("okRes", "errText", "say", "jobSaid", "issueNote", "issueAction", "followJob", "jobUrl")) + r"""
    var polled = []; function badges(){} var _clients = null, _opsFresh = false;
    function post(){ return Promise.resolve({ok: true, _status: 200, job_id: 'j9', message: 'Fetching — new reviews in a minute'}); }
    function pollJob(url){ polled.push(url); return Promise.resolve({ok: false, state: 'failed', result: {error: 'Google refused the token'}, status: 'error', _status: 200}); }
    (async function(){
      location.hash = '#overview';
      await issueAction('/admin/fetch-reviews/5', 'Sync now', "Simple EJ's", '');
      console.log(JSON.stringify({polled: polled, toasts: _toasts, routed: _routed}));
    })();
    """
    out = _node(js)
    assert out["polled"] == ["/admin/api/admin-jobs/j9"]
    assert out["toasts"][0] == ["Fetching — new reviews in a minute", ""]
    assert out["toasts"][1] == ["Google refused the token", "bad"]
    assert out["routed"] == 1                                      # reloaded once, after the job — not at once


def test_pos_sync_now_is_followed_and_the_modal_says_how_it_went(page):
    js = _page_js(page, ("okRes", "errText", "say", "jobSaid", "jobUrl", "statusSay", "posSyncFollow", "toastSync", "rpowerSync")) + r"""
    var _tRid = 5, _rpRid = 6; mk('m-toast-status'); mk('m-rpower-status');
    function post(path){ return Promise.resolve({ok: true, _status: 200, job_id: path.indexOf('toast') >= 0 ? 't1' : 'r1', message: 'Sync started.'}); }
    function pollJob(url){ return Promise.resolve(url.indexOf('t1') >= 0 ? {ok: true, state: 'ok', status: 'done', _status: 200}
                                                             : {ok: false, state: 'failed', result: {error: 'RPOWER: store offline'}, status: 'error', _status: 200}); }
    (async function(){
      await toastSync(); await rpowerSync();
      console.log(JSON.stringify({t: $('m-toast-status').textContent, r: $('m-rpower-status').textContent, toasts: _toasts,
                                   tc: $('m-toast-status').style.color, rc: $('m-rpower-status').style.color}));
    })();
    """
    out = _node(js)
    assert out["t"] == "Toast sync finished" and out["tc"] == "var(--green-t)"      # said in colour (V07)
    assert out["r"] == "RPOWER: store offline" and out["rc"] == "var(--red-t)"
    assert ["RPOWER: store offline", "bad"] in out["toasts"]


# ── W7: Operations reads fresh after a cap, a reinstate or an action ────────

def test_operations_redraws_from_fresh_reads_after_its_writes(page):
    assert "_opsFresh = true; route();" in _fn(page, "capSubmit")
    assert "_opsFresh = true; route();" in _fn(page, "capSet")
    assert "_opsFresh = true; supLoad();" in _fn(page, "supReinstate")
    a = _fn(page, "act")
    assert "location.hash.indexOf('#operations') === 0) _opsFresh = true" in a
    assert a.index("_opsFresh = true") < a.rindex("route()")


# ── C11/W15-f: modal forms and append-only notes send once ──────────────────

def test_a_double_click_sends_once_and_the_button_comes_back(page):
    js = _page_js(page, ("oneAtATime", "goBtn"), ("_inflight",)) + r"""
    var sent = 0, btn = {disabled: false}, release;
    function slow(){ sent++; return new Promise(function(res){ release = res; }); }
    (async function(){
      var a = oneAtATime('note:5', btn, slow), b = oneAtATime('note:5', btn, slow);
      var during = btn.disabled, second = await b;
      release({ok: true}); var first = await a;
      var c = await oneAtATime('note:5', btn, function(){ sent++; return Promise.reject(new Error('x')); }).then(null, function(e){ return e.message; });
      console.log(JSON.stringify({sent: sent, during: during, second: second, first: first.ok, after: btn.disabled, threw: c, free: !_inflight['note:5']}));
    })();
    """
    out = _node(js)
    assert out == {"sent": 2, "during": True, "second": None, "first": True, "after": False, "threw": "x", "free": True}
    for fn, key in (("capSubmit", "'cap'"), ("resolveSubmit", "'resolve'"), ("noteAdd", "'note:' + id"),
                    ("staffNoteAdd", "'staff-note:' + id"), ("brandSave", "'brand'")):
        assert "oneAtATime(" + key in _fn(page, fn), fn
    assert "occurrence_at:_resolve.occurrence_at" in _fn(page, "resolveSubmit")


# ── W6 / W15-d: what a billing write really did ─────────────────────────────

def test_mark_signed_says_what_each_send_did_and_a_skipped_welcome(page):
    js = _page_js(page, ("markSignedSaid",), ("SEND_KIND", "SEND_STATE")) + r"""
    console.log(JSON.stringify([
      markSignedSaid({sends: [{kind: 'payment_link', state: 'sent'}]}, {send_payment_link: true, send_welcome: true}),
      markSignedSaid({sends: [{kind: 'payment_link', state: 'retry', error: 'Resend 502'}, {kind: 'welcome', state: 'sent'}]}, {send_payment_link: true, send_welcome: true}),
      markSignedSaid({sends: []}, {send_payment_link: false, send_welcome: false})]));
    """
    out = _node(js)
    assert out[0] == {"text": "Contract marked signed — payment link sent · welcome not sent — the owner has signed in before, or has no login", "bad": True}
    assert out[1] == {"text": "Contract marked signed — payment link not sent yet — it is retried (Resend 502) · welcome sent", "bad": True}
    assert out[2] == {"text": "Contract marked signed", "bad": False}
    ms = _fn(page, "markSigned")
    assert "after: function(d){ var m = markSignedSaid(d, asked)" in ms and "asked = v;" in ms


def test_attaching_a_stripe_customer_says_the_reconcile_result(page):
    js = _page_js(page, ("attachSaid",)) + r"""
    console.log(JSON.stringify([attachSaid({ok: true, error: 'Stripe timed out'}), attachSaid({ok: true, mismatches: [{kind: 'status', detail: 'Stripe says past_due'}]}), attachSaid({ok: true, mismatches: []})]));
    """
    out = _node(js)
    assert out[0]["bad"] and "Stripe timed out" in out[0]["text"]
    assert out[1] == {"text": "Stripe customer attached — 1 mismatch with Stripe: Stripe says past_due", "bad": True}
    assert out[2] == {"text": "Stripe customer attached", "bad": False}
    assert "attachSaid(d)" in _fn(page, "attachCustomer")


# ── W15 client-side: a, e, g, h ─────────────────────────────────────────────

def test_the_small_wiring_defects(page):
    ev = _fn(page, "opsEvents")
    assert 'class="btn s w ev-save" onclick="evSave(' in ev and 'class="btn g s w" title=' in ev  # W15-a: support never sees them
    js = _page_js(page, ("principalLogin",)) + r"""
    console.log(JSON.stringify([
      principalLogin([{id: 3, is_active: 1, role: 'manager', email: 'm@x'}, {id: 7, is_active: 1, role: null, email: 'o@x'}]).id,
      principalLogin([{id: 2, is_active: 1, role: 'owner', email: '12@staff.invalid'}, {id: 9, is_active: 1, role: 'manager', email: 'm@x'}]).id,
      principalLogin([{id: 1, is_active: 0, role: 'owner', email: 'o@x'}])]));
    """
    assert _node(js) == [7, 9, None]                     # W15-e: a blank role is the owner, never the manager first
    assert "principalLogin(c.logins)" in _fn(page, "actionsMenu")
    off = _fn(page, "offboardHtml")
    assert "'Mark pending' : 'Undo'" in off and "OFF_DONE_STAYS[s.step]" in off                     # W15-g
    assert "gone.expires_on" in _fn(page, "staffNoteDelete")                                         # W15-h
    assert "fd.append('expires_on', expires)" in _fn(page, "staffNoteAdd")


# ── WS-2 follow-ups: suppressions, and the menu page with the notes ─────────

def test_reinstate_is_offered_only_where_the_server_allows_it_and_names_the_client(page):
    for fn in ("supLoad", "suppressionsHtml"):
        assert "reinstatable === false ? NOT_REINSTATABLE" in _fn(page, fn), fn
    assert "reinstate('${jsq(s.email)}','${jsq(c.name)}',${c.id})" in _fn(page, "suppressionsHtml")
    js = _page_js(page, ("okRes", "errText", "say", "reinstate")) + r"""
    var posted = []; function prompt(){ return 'bounced once, fixed'; }
    function post(path, body){ posted.push(body); return Promise.resolve({ok: false, _status: 409, refused: true,
      error: 'This address unsubscribed itself — only the person can undo that.'}); }
    (async function(){ await reinstate('a@b.c', "Simple EJ's", 5); console.log(JSON.stringify({posted: posted, toasts: _toasts, routed: _routed})); })();
    """
    out = _node(js)
    assert out["posted"] == [{"email": "a@b.c", "reason": "bounced once, fixed", "restaurant_id": 5}]
    assert out["toasts"] == [["This address unsubscribed itself — only the person can undo that.", "bad"]]
    assert out["routed"] == 0


def test_the_menu_page_an_extraction_read_is_saved_with_the_notes(page):
    js = _page_js(page, ("okRes", "errText", "busyBtn", "mnCountText", "mnCount", "mnSay", "mnSave", "mnUse"),
                  ("fmtN", "MENU_NOTES_MAX", "_mn")) + r"""
    var posted = [];
    function post(path, body){ posted.push(body); return Promise.resolve({ok: true, _status: 200, version: 8, saved: {menu_notes: body.menu_notes, menu_url: body.menu_url}}); }
    mk('mn-text').value = 'Old notes'; mk('mn-save'); mk('mn-status'); mk('mn-mine'); mk('mn-count'); mk('mx-text').value = 'Burger, fries';
    t = $('mn-text'); t.focus = function(){};
    _mn = {id: 5, name: "Simple EJ's", version: 7, base: 'Old notes', url: '/admin/client-settings/5', mine: null, baseUrl: null, newUrl: null, extractUrl: 'https://ejs.example/menu'};
    (async function(){
      mnUse(); await mnSave();
      $('mn-text').value = 'Burger, fries, shakes'; await mnSave();          // the next save sends the notes alone
      console.log(JSON.stringify({posted: posted, baseUrl: _mn.baseUrl}));
    })();
    """.replace("t = $('mn-text')", "var t = $('mn-text')")
    out = _node(js)
    assert out["posted"][0] == {"menu_notes": "Burger, fries", "expected_version": 7,
                                "base": {"menu_notes": "Old notes", "menu_url": None}, "menu_url": "https://ejs.example/menu"}
    assert out["posted"][1] == {"menu_notes": "Burger, fries, shakes", "expected_version": 8, "base": {"menu_notes": "Burger, fries"}}
    assert out["baseUrl"] == "https://ejs.example/menu"
    assert "_mn.extractUrl = r.menu_url || null" in _fn(page, "menuExtract")


# ── L2 / K-12: a rate over an empty base is "—" ─────────────────────────────

def test_retention_is_over_accounts_that_ever_paid_and_dash_over_none(page):
    js = _page_js(page, ("retentionOf", "readErr", "custCostPer"), ("PAYING_STATUSES",)) + r"""
    var trials = [{id: 1, status: 'trial'}, {id: 2, status: 'trial'}];
    console.log(JSON.stringify({
      none: retentionOf(trials),
      approx: retentionOf(trials.concat([{id: 3, status: 'active'}, {id: 4, status: 'canceled'}])),
      exact: retentionOf([{id: 1, status: 'canceled', converted_at: null}, {id: 2, status: 'active', converted_at: '2026-09-01'},
                          {id: 3, status: 'churned', converted_at: '2026-08-01'}, {id: 4, status: 'trial', converted_at: null}]),
      cost: custCostPer({ok: true, by_client: [{restaurant_id: 1, cost: 3}, {restaurant_id: 6, cost: 40}, {restaurant_id: null, cost: 9}]}, [{id: 1}, {id: 2}]),
      empty: custCostPer({ok: true, by_client: [{restaurant_id: 6, cost: 40}]}, []),
      failed: custCostPer({ok: false, _status: 500, error: 'x'}, [{id: 1}])}));
    """
    out = _node(js)
    assert out["none"]["rate"] is None and out["none"]["basis"] == "No account has paid yet"     # was "100% kept"
    assert out["approx"]["rate"] == 50 and "no conversion dates" in out["approx"]["basis"]
    assert out["exact"] == {"rate": 50, "kept": 1, "lost": 1, "basis": "Of 2 accounts that ever paid"}
    assert out["cost"]["per"] == 1.5                      # the demo (#6) and unattributed calls are not a customer's
    assert out["empty"]["per"] is None and out["failed"] is None
    a = _fn(page, "analytics")
    assert "retentionOf(real)" in a and "custCostPer(ai, live)" in a
    assert "t.cost / live.length" not in a and "Math.round(live.length / real.length * 100)" not in a
    assert "perAcct.per == null ? '—'" in a and "kept == null ? '—'" in a


def test_the_operations_sentence_ends_once(page):
    # Lead harness pass (admin audit 10/27/26): the server's reasons end in a
    # full stop, and the area's sentence read "at 1 client.." and "pos_sync….".
    js = _page_js(page, ("opsSentence",), ("esc",)) + r"""
    var sys = [{label: 'Jobs', tone: 'bad', sub: '106 jobs past their SLA: backup_db, pos_sync….'},
               {label: 'Integrations', tone: 'warn', sub: '1 integration failing at 1 client.'}];
    console.log(JSON.stringify([opsSentence(sys), opsSentence([sys[0]])]));
    """
    out = _node(js)
    assert out[0] == "<b>Jobs — 106 jobs past their SLA: backup_db, pos_sync…</b> · Integrations — 1 integration failing at 1 client."
    assert out[1] == "<b>Jobs — 106 jobs past their SLA: backup_db, pos_sync…</b>"
