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


# ── O9: stale data sources, and the fleet list's Data column ────────────────

DS_FNS = ("dsQuery", "dsStaleKnown", "dsChip", "dsHtml", "errBanner", "table", "renderTable", "pager",
          "parseTs", "durMin", "ago", "mdy", "fmtD", "fmtT", "fmtDT")


def test_data_sources_say_stale_filter_on_it_and_never_call_an_unjudged_row_ok():
    js = _js(DS_FNS, ("_dsf", "DS_CHIP", "clientLink")) + r"""
    var _dsp = 1;
    var rows = [{restaurant_id: 5, restaurant: 'EJ', label: 'POS', failures: 3, state: 'failing'},
                {restaurant_id: 5, restaurant: 'EJ', label: 'Reviews', failures: 0, state: 'stale', last_ok_at: '2026-10-20T10:00:00Z'},
                {restaurant_id: 6, restaurant: 'HR', label: 'POS', failures: 0, state: 'ok'}];
    var newer = {ok: true, rows: rows, page: 1, pages: 1, total: 3, summary: [{source: 'pos', label: 'POS', restaurants: 2, failing: 1, stale: 0}, {source: 'reviews', label: 'Reviews', restaurants: 1, failing: 0, stale: 1}]};
    var older = {ok: true, rows: rows, page: 1, pages: 1, total: 3, summary: [{source: 'pos', label: 'POS', restaurants: 2, failing: 1}, {source: 'reviews', label: 'Reviews', restaurants: 1, failing: 0}]};
    var q = [dsQuery()]; _dsf = 'stale'; q.push(dsQuery()); _dsf = 'failing'; q.push(dsQuery());
    _dsf = 'stale';
    console.log(JSON.stringify({q: q, newer: dsHtml(newer), older: dsHtml(older)}));
    """
    out = _node(js)
    assert out["q"] == ["/admin/api/data-sources?page=1", "/admin/api/data-sources?page=1&state=stale",
                        "/admin/api/data-sources?page=1&state=failing&failing=1"]
    n = out["newer"]
    assert 'class="chip bad">failing' in n and 'class="chip warn">stale' in n and 'class="chip ok">ok' in n
    assert "Stale <b>1</b>" in n and "isn't filtered" not in n
    o = out["older"]                                       # a server that doesn't report staleness yet
    assert 'class="chip ok">ok' not in o and "not failing" in o and "Stale <b>—</b>" in o
    assert "doesn't report stale sources yet — the list below isn't filtered" in o


def test_the_fleet_list_draws_a_labelled_data_column_and_never_says_never_for_a_missing_field():
    m = re.search(r"\nconst FLEET_STATE_CHIP.*?\nfunction _fleetDataCell\(r\)\{.*?\n\}", PAGE, re.S)
    js = STUBS + "function parseTs(t){ return t ? new Date(t) : null; } function ago(t){ return 'ago'; }\n" + m.group(0) + r"""
    console.log(JSON.stringify([_fleetDataCell({}), _fleetDataCell({freshness: {pos_state: null, reviews: null, pos: null}}),
      _fleetDataCell({freshness: {pos_state: {state: 'error', last_synced: '2026-10-27T10:00:00Z'}}})]));
    """
    missing, never, error = _node(js)
    assert "—" in missing and "never" not in missing           # not sent is not "never"
    assert "never" in never and "chip warn" in never
    assert "chip bad" in error
    row = _fn("custRow")
    assert "${_fleetDataCell(r)}" in row
    assert "<span>Active</span><span title=" in _fn("clientsView") and ">Data</span>" in _fn("clientsView")
    assert _decl("CUST_COLS").count("px") == 3                   # the Data column has its own width
    assert "${ago(last)}</span><span></span>" in _fn("brandRow")  # a brand row keeps the grid


# ── W15-h: the staff-note Undo puts every part back ─────────────────────────

def test_the_staff_note_undo_posts_the_stored_parts_and_an_older_server_still_gets_the_text():
    js = _js(("staffNoteAdd", "staffNoteDelete", "oneAtATime", "goBtn", "okRes", "errText", "say"), ("_inflight",)) + r"""
    var sent = [];
    function FormData(){ this.f = {}; } FormData.prototype.append = function(k, v){ this.f[k] = v; };
    function post(path, body){ sent.push(['json', path, body]); return Promise.resolve(path.indexOf('/delete') > 0 ? answer : {ok: true, appended: false, _status: 200}); }
    function api(path, o){ sent.push(['form', path, o.body.f]); return Promise.resolve({ok: true, _status: 200}); }
    var parts = [{text: 'no Sundays', expires_on: null}, {text: 'off by 4pm', expires_on: '2026-12-01'}];
    var answer;
    (async function(){
      answer = {ok: true, _status: 200, deleted: {employee_name: 'Ana', notes: 'no Sundays; off by 4pm', restaurant_id: 5, parts: parts}};
      await staffNoteDelete(5, 77, 'Ana', 'EJ');
      await new Promise(function(r){ setTimeout(r, 5); });
      answer = {ok: true, _status: 200, deleted: {employee_name: 'Bo', notes: 'mornings', restaurant_id: 5, expires_on: '2026-11-30'}};
      await staffNoteDelete(5, 78, 'Bo', 'EJ');
      await new Promise(function(r){ setTimeout(r, 5); });
      console.log(JSON.stringify(sent));
    })();
    """
    sent = _node(js)
    undo = [s for s in sent if s[1] == "/admin/staff-notes/5"]
    assert undo[0] == ["json", "/admin/staff-notes/5", {"employee_name": "Ana", "notes": "no Sundays; off by 4pm",
                                                        "parts": [{"text": "no Sundays", "expires_on": None},
                                                                  {"text": "off by 4pm", "expires_on": "2026-12-01"}]}]
    assert undo[1] == ["form", "/admin/staff-notes/5", {"employee_name": "Bo", "notes": "mornings", "expires_on": "2026-11-30"}]


# ── F12: the RPOWER store picker whenever stores come back ──────────────────

def test_the_rpower_store_picker_shows_beside_a_failed_bootstrap():
    js = _js(("rpowerSave", "rpowerPicker", "okRes", "errText", "statusSay", "pulse")) + r"""
    var _rpRid = 5, _clients = 1, answer;
    function post(){ return Promise.resolve(answer); }
    mk('rp-token').value = 'tok'; mk('m-rpower-status'); mk('rp-store'); mk('rp-stores');
    (async function(){
      answer = {ok: false, _status: 400, error: 'RPOWER refused the store binding.', stores: [{store_mid: '12', cg: '3', name: "Simple EJ's"}]};
      await rpowerSave();
      var failed = {shown: _els['rp-stores'].style.display, opts: _els['rp-store'].children.map(function(o){ return [o.value, o.textContent]; }),
                    said: _els['m-rpower-status'].textContent, color: _els['m-rpower-status'].style.color};
      _els['rp-stores'].style.display = 'none'; _els['rp-store'].children = [];
      answer = {ok: true, _status: 200, connected: true, message: 'Connected to EJ.'};
      await rpowerSave();
      console.log(JSON.stringify({failed: failed, okShown: _els['rp-stores'].style.display, okSaid: _els['m-rpower-status'].textContent}));
    })();
    """
    out = _node(js)
    f = out["failed"]
    assert f["shown"] == "block" and f["opts"] == [["12", "Simple EJ's (cg 3)"]]
    assert f["said"].startswith("RPOWER refused the store binding.") and "Pick the store below" in f["said"]
    assert f["color"] == "var(--red-t)"
    assert out["okShown"] == "none" and out["okSaid"] == "Connected to EJ."
    assert "rpowerPicker(d)" in _fn("rpowerStatus")            # a status that names stores shows them too
    assert "posSyncFollow(d.job_id, 'RPOWER'" in _fn("rpowerSync")   # Sync now is followed (round 1, W5)


# ── O10: the Deployment card draws the release verdict ──────────────────────

def test_the_deployment_card_says_the_release_verdict_and_its_lines():
    js = _js(("relVerdict", "engDeploy", "pill", "unknownV", "parseTs", "mdy", "fmtT", "fmtDT"), ("REL",)) + r"""
    var sys = {build: 'abc1234def', env: 'production', db: 'postgres'};
    var mkS = function(sup){ return {boots: {crash_loop: {boots_last_hour: 1}, recent: []}, supervisor: sup}; };
    console.log(JSON.stringify({
      pending: engDeploy(sys, null, mkS({scheduler_thread: 'alive', release: null})),
      ok: engDeploy(sys, null, mkS({release: {build: 'abc', verdict: 'ok', urgent: [], warn: []}})),
      hold: engDeploy(sys, null, mkS({release: {build: 'abc', verdict: 'hold', urgent: [], warn: ['3 schedule_generate rows failed in the window']}})),
      bad: engDeploy(sys, null, mkS({release: {build: 'abc', verdict: 'regressed', urgent: ['5xx rate 12% against 0.4% before'], warn: []}})),
      nosup: engDeploy(sys, null, mkS(null)),
      older: engDeploy(sys, null, mkS({scheduler_thread: 'alive'})),
      failed: engDeploy(sys, 'HTTP 500', {})
    }));
    """
    out = _node(js)
    assert 'class="pill neu' in out["pending"] and ">pending<" in out["pending"]
    assert 'class="pill good' in out["ok"] and ">ok<" in out["ok"]
    assert ">hold the next deploy<" in out["hold"] and "color:var(--amber-t)" in out["hold"] and "schedule_generate" in out["hold"]
    assert ">regressed — roll back<" in out["bad"] and "color:var(--red-t);font-size:12.5px\">5xx rate" in out["bad"]
    for k in ("nosup", "older", "failed"):                      # unknown, never ok and never pending
        assert ">unknown<" in out[k], k
    assert "No supervisor in this process." in out["nosup"] and "doesn&#39;t report release health" in out["older"]


# ── P-05: one read queue, two at a time, emptied by navigation ──────────────

QUEUE_FNS = ("isWrite", "readDone", "readNow", "readTurn", "readPump", "readQueueDrop", "api", "apiAll")
QUEUE_DECLS = ("STALE", "_seq", "NO_ANSWER", "_readCtl", "_reauthP", "READ_MAX", "API_ALL_MAX")


def test_every_page_read_shares_one_queue_of_two_and_navigation_empties_it():
    js = _js(QUEUE_FNS, QUEUE_DECLS) + r"""
    function reauth(){ return Promise.resolve(false); }
    var now = 0, peak = 0, fetched = [];
    var fetch = function(path, o){
      var counted = path.indexOf('/admin/api/admin-jobs') < 0 && path.indexOf('/admin/api/badges') < 0;
      fetched.push(path); if(counted){ now++; peak = Math.max(peak, now); }
      return new Promise(function(res, rej){
        if(o.signal) o.signal.addEventListener('abort', function(){ if(counted) now--; rej(new Error('aborted')); });
        setTimeout(function(){ if(counted) now--; res({status: 200, headers: {get: function(){ return null; }}, json: function(){ return Promise.resolve({ok: true, p: path}); }}); }, 10);
      }); };
    (async function(){
      _readCtl = new AbortController(); _seq = 1;
      // an apiAll of three and two lazy panels, asked at once, plus a poll and the rail
      var all = apiAll(['/a1', '/a2', '/a3'], 2), l1 = api('/lazy1'), l2 = api('/lazy2');
      var poll = api('/admin/api/admin-jobs/9', {method: 'GET'}), rail = api('/admin/api/badges', {method: 'GET'});
      var got = await Promise.all([all, l1, l2, poll, rail]);
      var peak1 = peak;
      // the admin moves on while reads are queued: the queued ones never go out
      fetched = []; peak = 0;
      var q = [api('/b1'), api('/b2'), api('/b3'), api('/b4')].map(function(p){ return p.then(function(){ return 'drawn'; }, function(e){ return e.message; }); });
      await new Promise(function(r){ setTimeout(r, 1); });
      _readCtl.abort(); _readCtl = new AbortController(); _seq++; readQueueDrop();
      var after = await Promise.all(q);
      console.log(JSON.stringify({peak1: peak1, got: got.map(function(x){ return Array.isArray(x) ? x.map(function(y){ return y.p; }) : x.p; }),
                                  fetched2: fetched, after: after, left: _readIn}));
    })();
    """
    out = _node(js)
    assert out["peak1"] == 2                                   # apiAll and the lazy panels share the two slots
    assert out["got"] == [["/a1", "/a2", "/a3"], "/lazy1", "/lazy2", "/admin/api/admin-jobs/9", "/admin/api/badges"]
    assert out["fetched2"] == ["/b1", "/b2"]                   # b3 and b4 waited, then left with the page
    assert out["after"] == ["__stale__"] * 4 and out["left"] == 0
    r = _fn("route")
    assert r.index("++_seq") < r.index("readQueueDrop()")


# ── C5: one helper each ─────────────────────────────────────────────────────

def test_the_duplicated_helpers_are_one_each():
    for gone in ("function readFailed(", "function rpPct(", "function aiPct(", "function isoMdy(", "function perDay("):
        assert gone not in PAGE, gone
    code = re.sub(r"//[^\n]*", "", PAGE)                     # comments may name what was folded
    for gone in ("readFailed(", "rpPct(", "aiPct(", "isoMdy("):
        assert gone not in code, gone
    js = _js(("readErr", "sharePct", "parseTs", "durMin", "ago", "minsAgo", "agoWord", "dayKey", "mdy", "dayOf", "unknownV", "figOr")) + r"""
    var iso = function(m){ return new Date(Date.now() - m * 60000).toISOString(); };
    var d = {ok: true, unavailable: ['restaurants'], query_errors: [{query: 'ai_usage', error: 'timeout'}]};
    console.log(JSON.stringify({
      unavailable: readErr(d, ['restaurants']), q: readErr(d, ['ai_usage']), fine: readErr(d, ['email_log']),
      fig: figOr(d, ['restaurants'], '12'), figOk: figOr(d, ['email_log'], '12'),
      pct: [sharePct(0.1234), sharePct(null), sharePct(1)],
      words: [agoWord(iso(0.2)), agoWord(iso(41020)), agoWord(null), minsAgo(180)],
      key: dayKey(new Date(2026, 8, 5)), event: mdy(dayOf(String('2026-10-28' || '').slice(0, 10))), none: mdy(dayOf(''))
    }));
    """
    out = _node(js)
    assert out["unavailable"] == "restaurants: unavailable"          # the stricter rule: unavailable counts
    assert out["q"] == "ai_usage: timeout" and out["fine"] is None
    assert "unknown" in out["fig"] and out["figOk"] == "12"
    assert out["pct"] == ["12.3%", "—", "100%"]
    assert out["words"] == ["just now", "28d ago", "—", "3h ago"]
    assert out["key"] == "2026-09-05" and out["event"] == "10/28/26" and out["none"] == "—"


# ── C10, V09: labels that say what they count, a figure said once ───────────

def test_the_overview_ai_cell_says_every_vendor_and_the_client_ai_tab_says_spend_once():
    ov = _fn("overview")
    strip = ov[ov.index('<section class="card strip">'):]
    strip = strip[:strip.index("</section>")]
    assert "'AI &amp; data APIs'" in strip and "'AI spend'" not in strip
    assert "k.ai_today_label" in strip                         # "since midnight Central", the server's window
    cv = _fn("clientView")
    assert "tab === 'messages' && _cmsg === 'ai' ? '' : stripDiv('AI · 30 days'" in cv
    assert "stripDiv('Cost · 30 days'" in _fn("cMessages")     # the tab keeps its own strip


def test_the_page_keeps_no_heartbeat_tiers_of_its_own():
    # C10: the heartbeat's stale minutes come from the server (round 1, W2):
    # the Jobs card shows heartbeat.stale_after_minutes and judges by its flags.
    jobs = _fn("opsJobs")
    assert "durMin(h.stale_after_minutes)" in jobs and "h.stale" in jobs
    for literal in ("> 15", ">= 15", "> 60", ">= 60"):
        assert literal not in jobs and literal not in _fn("railHealth") and literal not in _fn("engSched"), literal


# ── V19, V23, V30: one class for a padded card, one dot, one pill, checks ───

def test_a_padded_card_is_one_class():
    assert ".pad{padding:16px 18px;display:flex;flex-direction:column;gap:12px}" in CSS
    assert "padding:16px 18px;display:flex;flex-direction:column;gap:1" not in PAGE[PAGE.index("</style>"):]
    assert PAGE.count('class="card pad"') >= 45


def test_one_dot_one_pill_and_the_house_check_disc():
    body = re.sub(r"//[^\n]*", "", PAGE[PAGE.index("</style>"):])     # code, not comments
    assert 'class="b ' not in body                                     # .b folded into .pill
    assert "●" not in body                                             # no glyph dots
    assert not re.search(r'<i style="[^"]*border-radius:50%', body)    # no inline dots
    assert "✓" not in body                                             # checks are the disc
    for rule in (".dot.info{", ".dot.run{", ".dot.ring{", ".pill.ok", ".pill.off", ".pill.info", ".pill.ember",
                 ".cv-ok{background:radial-gradient(circle at 35% 30%,var(--hb-pop2),var(--hb-pop) 72%)",
                 ".cv-warn::before{content:'!'}", ".cv-bad{"):
        assert rule in CSS, rule
    root = CSS[CSS.index(":root{"):CSS.index("}", CSS.index(":root{"))]
    for tok in ("--hb-pop:#3a9f68", "--hb-wpop:#d29a2c", "--hb-bpop:#c9443f"):   # the dark values (dark only)
        assert tok in root, tok
    assert '<span class="cv-ok"' in _fn("billingHealth") and '<span class="cv-ok"' in _fn("cOverview")
    js = STUBS + _decl("HEALTH") + _decl("hb") + _decl("INT") + _decl("intb") + _decl("RING_DOT") + r"""
    console.log(JSON.stringify([hb('critical'), intb('error', 'Error'), RING_DOT.unk]));
    """
    h, i, unk = _node(js)
    assert h == '<span class="pill bad"><i></i>Critical</span>' and i == '<span class="pill bad"><i></i>Error</span>'
    assert unk == "off"
    assert 'class="dot ring ${JOB_DOT[j.state]' in _fn("jobRow") and 'class="dot ring ${c}"' in _fn("changesList")
