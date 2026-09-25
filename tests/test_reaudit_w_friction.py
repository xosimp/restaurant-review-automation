"""Re-audit fix round (9/25/26), workstream W — web friction: F1-1 … F1-15
and the web halves of F2-9, F2-16, F2-17.

Server behaviour runs against a scratch database. The dashboard's router
block and the pieces of its inline JS that carry the fix run under node
against small stubs (test_nav_router_web's harness); what must hold
everywhere is pinned against the source.
"""
import json
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

import auth
import client_api
import models
from models import Restaurant, create_restaurant, get_conn
from test_nav_router_web import STUB, _block, _src


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import action_queue, issues, ask_cavnar_tools, push
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, action_queue, client_api, issues, ask_cavnar_tools, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Friction W Co")
    kw.setdefault("owner_email", "o@x.test")
    for m in ("module_reviews", "module_labor", "module_inventory", "module_marketing"):
        kw.setdefault(m, 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _user(rid, role="client", uid=1):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "role": role, "is_admin": False,
            "username": f"u{uid}", "email": f"u{uid}@x.test"}


def _review(db_path, rid, rating=5, text="Lovely night", draft="Thank you so much for coming in."):
    import uuid
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                       "draft_response, review_date, fetched_at, processed, response_status, urgency) "
                       "VALUES (?,'google',?,'Dana',?,?,?,date('now'),datetime('now'),1,'drafted','normal')",
                       (rid, uuid.uuid4().hex[:12], rating, text, draft))
    conn.commit()
    rv = cur.lastrowid
    conn.close()
    return rv


def _labor_world(db_path, rid):
    today = date.today()
    conn = get_conn(db_path)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, generated_at, schedule_csv) "
                       "VALUES (?, ?, ?, datetime('now'), 'x')",
                       (rid, (today + timedelta(days=2)).isoformat(), (today + timedelta(days=8)).isoformat())).lastrowid
    sr = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                      "kind, status) VALUES (?, ?, 'Ana', ?, '16:00', 'drop', 'pending')",
                      (rid, hid, (today + timedelta(days=2)).isoformat())).lastrowid
    to = conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                      "VALUES (?, 'Bo', ?, ?, 'pending')",
                      (rid, (today + timedelta(days=5)).isoformat(), (today + timedelta(days=6)).isoformat())).lastrowid
    conn.commit(); conn.close()
    return hid, sr, to


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _nav(js_body):
    return _node(STUB + _block("cav-nav") + "\n" + js_body)


# ── F1-1: a multi-location owner can get back to one location ────────────────

def test_f1_1_picking_a_location_is_remembered_and_item_navs_leave_the_group_view():
    src = _src()
    sw = re.search(r"window\.switchLocation = function\(rid(?:, onFail)?\) \{.*?\n\};", src, re.S).group(0)
    assert "localStorage.setItem('cavnar_hb_scope', 'location')" in sw, "a switch remembers the pick"
    assert "if(+rid==={{ restaurant.id|int }}){hbScope('location');return;}" in src, \
        "the location already on opens its own Home without a reload"
    got = _nav("""
var calls = [];
window.hbScopeNow = function () { return 'group'; };
window.hbScope = function (s) { calls.push(s); };
cavNav('issue/9');
console.log(JSON.stringify(calls));""")
    assert got == ["location"], "an issue link leaves the group view, which draws no issue rows"


# ── F1-2: one panel on first load ────────────────────────────────────────────

def test_f1_2_only_home_renders_active():
    src = _src()
    active = re.findall(r'<div class="panel[^"]*active[^"]*" id="(panel-[a-z]+)"', src)
    assert active == ["panel-home"], active
    assert "'active' if not mod_reviews" not in src


# ── F1-3 / F2-16: outward sends confirm first; routes only for who may ──────

def test_f1_3_queue_sends_carry_a_confirm_and_follow_the_route_permission(db_path):
    import action_queue
    rid = _rid(db_path)
    hid, sr, to = _labor_world(db_path, rid)
    own = {i["key"]: i for i in action_queue.items(rid, viewer=_user(rid), db_path=db_path)["items"]}
    send = own[f"schedule_unsent:{hid}"]["action"]
    assert send["confirm"] == {"action": "publish_schedule", "args": {"schedule_id": hid}}
    appr = own[f"shift_request:{sr}"]["action"]
    assert appr["confirm"] == {"action": "decide_shift_request", "args": {"request_id": sr, "decision": "approve"}}
    assert appr["alt"]["confirm"]["args"]["decision"] == "deny"
    assert own[f"time_off:{to}"]["action"]["confirm"]["action"] == "decide_time_off"
    # A member drafts but may not send (no SCHEDULE_PUBLISH): Open it, no route.
    mem = {i["key"]: i for i in action_queue.items(rid, viewer=_user(rid, "member", 3), db_path=db_path)["items"]}
    msend = mem[f"schedule_unsent:{hid}"]["action"]
    assert "route" not in msend and "confirm" not in msend and msend["label"] == "Open it"
    assert msend["nav"] == f"schedule/{hid}"
    assert action_queue._may({"role": "support"}, "schedule.draft") is False


def test_f1_3_decide_proposals_need_the_decide_permission(db_path):
    import command_center
    rid = _rid(db_path)
    _hid, _sr, to = _labor_world(db_path, rid)
    out, status = command_center.propose(_user(rid, "manager", 2), "decide_time_off",
                                         {"request_id": to, "decision": "approve"})
    assert status == 200 and out["proposal"]["route"]["web"] == f"/api/labor/time-off/{to}/decide"
    assert command_center._extra_permission("decide_shift_request") == "schedule.draft"


def test_f1_3_and_f1_4_home_draws_one_button_and_confirms_outward_sends():
    src = _src()
    ro = re.search(r"function renderOpen\(acts\)\{.*?\n  \}", src, re.S).group(0)
    assert "hbActionDo(" not in ro, "the row's action is drawn once, by hbQueueActs"
    qa = re.search(r"function hbQueueActs\(a\)\{.*?\n  \}", src, re.S).group(0)
    assert qa.index("a.confirm&&a.confirm.action") < qa.index("else if(a.route)"), "a confirm wins over a route"
    ca = re.search(r"function hbConfirmAct\(btn\)\{.*?\n  \}", src, re.S).group(0)
    assert "/api/command/propose" in ca and "cavPropCard(" in ca
    assert "hbActionDo" not in ca, "no fallback that posts without the card"


# ── F1-5: the palette's chat result opens that chat ──────────────────────────

def test_f1_5_the_ask_module_handler_reads_conversation_from_the_query():
    src = _src()
    m = re.search(r"document\.addEventListener\('DOMContentLoaded', function\(\) \{\n  if \(typeof cavNavRegister "
                  r"!== 'function'\) return;\n  cavNavRegister\('ask'.*?\n\}\);\n", src, re.S)
    assert m, "the Ask module's handler"
    got = _nav("""
var opened = [];
window.askOpenConversation = function (id) { opened.push(id); };
var askOpenConversation = window.askOpenConversation;
function toggleAskCavnar() {}
add('ask-cavnar-panel');
""" + m.group(0) + """
fire('DOMContentLoaded');
cavNav('ask?conversation=42'); cavNav('ask/7');
console.log(JSON.stringify(opened));""")
    assert got == [42, 7]


# ── F1-6: one navigation per click; a section of the open module replaces ───

def test_f1_6_a_rail_click_inside_the_open_module_routes_once_and_adds_no_back_step():
    got = _nav("""
var sec = new El('lab-req', {'data-nav': 'labor/requests'}); sections.push(sec);
cavNav('labor');
var before = hist.pushes.slice();
cavNav('labor/requests');
console.log(JSON.stringify({tabs: _log.filter(function (x) { return x.indexOf('tab:') === 0; }),
  pushes: hist.pushes, before: before, hash: loc.hash}));""")
    assert got["tabs"] == ["tab:labor"], "the open module is not switched to again"
    assert got["pushes"] == got["before"] == ["#labor"], "a section of the open module replaces"
    assert got["hash"] == "#labor/requests"
    src = _src()
    assert src.count("closest('[data-nav-go]')") == 1, "one listener routes [data-nav-go]"
    assert "[data-attn-more],[data-nav-go]" not in src


# ── F1-7: previews and other logins' proposals stay out of Still open ───────

def test_f1_7_the_queue_lists_only_unanswered_ask_proposals_this_login_may_open(db_path):
    import action_queue, command_center
    rid = _rid(db_path)
    _review(db_path, rid)
    cmd = command_center.propose(_user(rid), "approve_all_reviews", {})[0]["proposal"]["proposal_id"]
    ask = models.log_ask_action(rid, "generate_schedule", summary="Generate next week's schedule",
                                outcome="proposed", user_id=1, surface="ask", db_path=db_path)
    food = models.log_ask_action(rid, "send_supplier_order", summary="Email the order", outcome="proposed",
                                 user_id=2, surface="ask", db_path=db_path)
    own = {i["key"] for i in action_queue.items(rid, viewer=_user(rid), db_path=db_path)["items"]}
    assert f"ask:{ask}" in own and f"ask:{cmd}" not in own, "a palette/Home card is never a queue item"
    mgr = {i["key"] for i in action_queue.items(rid, viewer=_user(rid, "manager", 2), db_path=db_path)["items"]}
    assert f"ask:{ask}" not in mgr, "the owner's proposal is not the manager's"
    assert f"ask:{food}" not in mgr, "not one this login may not run (no Food Cost)"


# ── F1-8: "Open it" rebuilds the same card ───────────────────────────────────

def test_f1_8_a_stored_request_decision_reopens_on_its_request(db_path):
    import command_center
    rid = _rid(db_path)
    _hid, _sr, to = _labor_world(db_path, rid)
    pid = models.log_ask_action(rid, "decide_time_off", summary="Approve Bo's time off",
                                body={"decision": "approve"}, outcome="proposed", user_id=1,
                                target={"request_id": to}, db_path=db_path)
    out, status = command_center.reopen(_user(rid), pid)
    assert status == 200, out
    assert out["proposal"]["route"]["web"] == f"/api/labor/time-off/{to}/decide"
    args = command_center._stored_args("apply_invoice_lines",
                                       {"body": '{"use_checked": true}', "target": '{"import_id": 9}', "summary": ""})
    assert args["import_id"] == 9, "an invoice card keeps its invoice, not the newest"


def test_f1_8_build_proposal_names_its_target(db_path):
    import ask_cavnar_tools
    rid = _rid(db_path)
    _hid, sr, _to = _labor_world(db_path, rid)
    p = ask_cavnar_tools.build_proposal("decide_shift_request", {"request_id": sr, "decision": "deny"},
                                        restaurant_id=rid)
    assert p["target"] == {"request_id": sr} and "request_id" not in p["body"]


# ── F1-9: the card posts what it listed, and settles in the same request ────

def test_f1_9_approve_all_posts_only_the_listed_replies(db_path, monkeypatch):
    import ask_cavnar_tools
    monkeypatch.setattr(client_api, "_attempt_google_post", lambda *a, **k: (False, None), raising=False)
    rid = _rid(db_path)
    a, b = _review(db_path, rid), _review(db_path, rid)
    p = ask_cavnar_tools.build_proposal("approve_all_reviews", {}, restaurant_id=rid)
    assert sorted(p["body"]["review_ids"]) == sorted([a, b])
    assert any(f["key"] == "review_ids" for f in p["fields_shown"]), "the client posts it"
    late = _review(db_path, rid)
    out, status = client_api._do_approve_all(rid, review_ids=p["body"]["review_ids"])
    assert status == 200 and out["approved"] == 2
    conn = get_conn(db_path)
    st = conn.execute("SELECT response_status FROM reviews WHERE id=?", (late,)).fetchone()[0]
    conn.close()
    assert st == "drafted", "a draft that landed after the card waits for the next one"
    assert client_api._do_approve_all(rid, review_ids=[])[0]["approved"] == 0, "an empty card posts nothing"


def test_f1_9_the_preview_holds_what_the_route_would_reword(db_path, monkeypatch):
    import ask_cavnar_tools, drafter
    rid = _rid(db_path)
    keep = _review(db_path, rid, draft="Thank you for coming.")
    _review(db_path, rid, draft="REWORD ME")
    real = drafter.check_reply
    monkeypatch.setattr(drafter, "check_reply",
                        lambda d, r=None, **k: (None, "Reworded.") if d == "REWORD ME" else real(d, r, **k))
    go, held = ask_cavnar_tools.bulk_publish_preview(rid)
    assert [r["id"] for r in go] == [keep] and held == 1


def test_f1_9_a_confirmed_action_settles_its_proposal_in_the_same_request(db_path, monkeypatch):
    from flask import Flask
    import command_center
    rid = _rid(db_path)
    user = _user(rid)
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    app = Flask(__name__)

    def run(path, pid, payload):
        with app.test_request_context(path, method="POST", headers={command_center.PROPOSAL_HEADER: str(pid)}):
            resp = app.response_class(json.dumps(payload), mimetype="application/json")
            return json.loads(command_center.settle_confirmed(resp).get_data(as_text=True))

    pid = models.log_ask_action(rid, "approve_all_reviews", summary="Approve and post", outcome="proposed",
                                user_id=1, surface="command", db_path=db_path)
    assert "proposal_settled" not in run("/api/labor/publish-schedule", pid, {"ok": True}), "another route"
    assert "proposal_settled" not in run("/api/reviews/approve-all", pid, {"ok": False})
    assert run("/api/reviews/approve-all", pid, {"ok": True, "approved": 2})["proposal_settled"] is True
    assert models.get_ask_proposal(rid, pid, db_path=db_path)["settled"]["outcome"] == "confirmed"
    def said():
        conn = get_conn(db_path)
        n = conn.execute("SELECT COUNT(*) FROM ask_cavnar_messages WHERE restaurant_id=?", (rid,)).fetchone()[0]
        conn.close()
        return n
    assert said() == 0, "a palette card's answer is written into no Ask chat (F1-15)"
    chat = models.log_ask_action(rid, "approve_all_reviews", summary="Approve and post", outcome="proposed",
                                 user_id=1, surface="ask", db_path=db_path)
    assert run("/mobile/api/reviews/approve-all", chat, {"ok": True})["proposal_settled"] is True
    assert said() == 1, "a chat's own proposal still lands in its chat"
    src = _src()
    assert "opts.headers['X-Cavnar-Proposal'] = String(p.proposal_id)" in src
    assert "if (!d.proposal_settled) _recordAskCavnarAction(p, 'confirmed');" in src


# ── F1-10 / F1-11: the bell at the right location ────────────────────────────

def test_f1_10_a_cross_location_open_is_recorded_after_the_switch():
    src = _src()
    fn = re.search(r"function openNotification\(n\) \{.*?\n  \}", src, re.S).group(0)
    assert fn.index("switchLocation(+n.restaurant_id)") < fn.index("markOpened(n)"), \
        "no open is posted from the location being left"
    assert "cavnar_pending_open" in fn
    assert "sessionStorage.getItem('cavnar_pending_open')" in src and "+o.rid === _here" in src


def test_f1_11_mark_all_read_covers_every_location_in_scope(db_path, monkeypatch):
    from flask import Flask
    rid = _rid(db_path)
    other = _rid(db_path, name="Second")
    user = _user(rid, "owner", 7)
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(client_api, "_notification_locations",
                        lambda r, v=None, scope=None: [(rid, "A"), (other, "B")] if scope == "group" else [(rid, None)])
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    r = app.test_client().get("/api/notifications?scope=group")
    assert r.status_code == 200
    conn = get_conn(db_path)
    seen = {x[0] for x in conn.execute("SELECT restaurant_id FROM notification_reads WHERE user_id=7").fetchall()}
    conn.close()
    assert seen == {rid, other}


# ── F1-12: new reviews on a filtered inbox ───────────────────────────────────

def test_f1_12_new_reviews_come_from_the_lists_own_query_and_only_newer_ones():
    src = _src()
    chunk = re.search(r"var _rvKnownTotal=.*?\nwindow\.rvShowNew=", src, re.S).group(0)
    chunk = re.sub(r"\{\{.*?\}\}", "5", chunk).replace("\nwindow.rvShowNew=", "\n")
    js = r"""
var _log = [], urls = [], _rvOffset = 20;
function Card(id){ this.id = id; this.style = {}; }
var list = {attrs: {'data-list-key': 'all|salmon|', 'data-total': '3'}, kids: [new Card('rc-10'), new Card('rc-8')],
  getAttribute: function (k) { return this.attrs[k]; }, setAttribute: function (k, v) { this.attrs[k] = String(v); },
  querySelectorAll: function () { return this.kids; },
  insertBefore: function (c) { this.kids.unshift(c); }, firstChild: null};
var els = {'new-reviews-banner': {style: {}}, 'new-reviews-text': {textContent: ''}};
var document = {getElementById: function (id) {
  if (els[id]) return els[id];
  for (var i = 0; i < list.kids.length; i++) if (list.kids[i].id === id) return list.kids[i];
  return null; }};
function _rvList(){ return list; }
function _rvCardsFrom(ids){ var o = []; for (var i = 0; i < ids.length; i++) o.push(new Card(ids[i])); return o; }
function filterReviews(){}
function apiJson(r){ return r; }
function fetch(u){ urls.push(u); return {then: function (f) { var d = f({ok: true, html: ['rc-12', 'rc-10', 'rc-5']});
  return {then: function (g) { g(d); return {catch: function () {}}; }}; }}; }
""" + chunk + r"""
rvNoteTotal(6);
console.log(JSON.stringify({urls: urls, ids: list.kids.map(function (c) { return c.id; }), off: _rvOffset,
  text: els['new-reviews-text'].textContent}));"""
    got = _node(js)
    assert got["urls"] and "search=salmon" in got["urls"][0], "the list's own search"
    assert got["ids"] == ["rc-12", "rc-10", "rc-8"], "an old non-matching card is not inserted"
    assert got["off"] == 21 and got["text"].startswith("1 new review")
    assert "_rvKnownTotal={{ (rstats.total|int)" in src, "the baseline is the page's own total"


# ── F1-13: alert emails open the item, at its location ───────────────────────

def test_f1_13_alert_links_open_the_item_through_its_nav():
    import notify
    assert notify.alert_url("order_send_held", rid=4).endswith("/?nav=inventory/order&loc=4")
    assert "/?nav=review/12" in notify.alert_url("1star", review_id=12)
    assert "?nav=labor/schedule" in notify.alert_url("coverage")
    assert "nav=ask" not in notify.alert_url("morning_brief"), "a click in an email never asks a model"
    src = _src()
    body = re.search(r"function checkTabParam\(\)\{.*?\n\}", src, re.S).group(0)
    assert "params['delete']('tab')" in body, "?tab= no longer sticks across reloads"
    assert "sessionStorage.setItem('cavnar_pending_nav',path)" in body and "switchLocation(loc)" in body


# ── F1-14: a hidden section is waited for ────────────────────────────────────

def test_f1_14_a_hidden_section_scrolls_once_it_is_shown():
    got = _nav("""
var sec = new El('fc2-count', {'data-nav': 'inventory/count'}); sections.push(sec); sec.hidden = true;
add('tab-inventory'); var p = add('panel-inventory'); p.classList.add('panel');
cavNav('inventory/count');
var early = _log.indexOf('scroll:fc2-count');
sec.hidden = false;
var t = window._timers || []; for (var i = 0; i < t.length; i++) if (t[i].fn) t[i].fn();
console.log(JSON.stringify({early: early, late: _log.indexOf('scroll:fc2-count')}));""")
    assert got["early"] == -1, "not scrolled to while hidden"
    assert got["late"] >= 0, "scrolled once the module shows it"


# ── F1-15: the smaller defects ───────────────────────────────────────────────

def test_f1_15_search_matches_percent_literally(db_path, monkeypatch):
    import command_center
    rid = _rid(db_path)
    _review(db_path, rid, text="Great pasta")
    _review(db_path, rid, text="Half price 50% off wine")
    got = command_center._search_reviews(rid, "%")
    assert len(got) == 1 and "50%" in got[0][1]["subtitle"]


def test_f1_15_client_details_are_fixed_at_the_source():
    src = _src()
    # the palette: a location once, queue rows say Open, one Home reload
    assert "c.id.indexOf('location:') === 0)) continue;" in src
    assert "tag: 'Open', nav: a.nav || null" in src
    assert "window.hbLoad(true);\n        acts = null;" not in src
    assert "if(_hbLoading){_hbAgain=" in src, "a load asked for mid-load runs after it"
    # the bell: M/D/YY days, a badge that refreshes
    day = re.search(r"function dayGroup\(iso\) \{.*?\n  \}", src, re.S).group(0)
    assert "'Jan','Feb'" not in day and "(d.getMonth() + 1) + '/' + d.getDate()" in day
    assert "setInterval(function() { if (!document.hidden) refreshBadge(); }" in src
    # issues past four are reachable
    assert "data-iss-more" in src and "if (row && row.hidden) row.hidden = false;" in src
    # the inbox filter and search survive a reload
    assert "function rvKeepAddress(){" in src and "rvKeepAddress();" in re.search(
        r"function setRF\(f,btn\)\{.*?\n\}", src, re.S).group(0)


# ── F2-9 (web): acknowledge what was shown, by key ───────────────────────────

def test_f2_9_the_web_acknowledges_the_warnings_it_showed_by_key():
    src = _src()
    fn = re.search(r"function cavBlockers\(list, keys\) \{.*?\n\}", src, re.S).group(0)
    got = _node(fn + """
console.log(JSON.stringify([cavBlockers([{key: 'a', text: 'A'}, {key: 'b', text: 'B'}]),
  cavBlockers(['A', 'B'], ['a', 'b']), cavBlockers(['A'])]));""")
    assert got[0] == {"texts": ["A", "B"], "keys": ["a", "b"]}
    assert got[1] == {"texts": ["A", "B"], "keys": ["a", "b"]}
    assert got[2] == {"texts": ["A"], "keys": None}, "an older server's list acknowledges with true"
    assert "acknowledge: ack ? (keys && keys.length ? keys.slice() : true) : false" in src
    assert "b.acknowledge=cb.keys||true" in src


# ── F2-17 (web): the send button and toast say what went out ─────────────────

def test_f2_17_waiting_send_names_who_it_reaches_and_what_happened():
    src = _src()
    assert "'Send to ' + reach.total + ' staff'" not in src
    assert "(reach.reachable ? 'Send to ' + reach.reachable + ' staff' : 'Publish to the staff portal')" in src
    ws = re.search(r"window\.lb2WaitSend = function \(hid, btn\) \{.*?\n  \};", src, re.S).group(0)
    assert "d.already_published" in ws and "d.portal_only" in ws
