"""Fix round, UI wave 3 — the admin console shows what the integration wave
added (templates/admin.html):

- issue texts (#91) on Operations → Push & alerts and on the client page's
  Texts: given up, retrying, not texted by choice; each given-up text in red
  with its reason, the owner told instead; a failed read is "unknown";
- each alert's channels (#14) wherever alert_log rows are listed (the fleet's
  Recent alerts, the client's Alerts fired);
- in flight (#59) on the email delivery, apart from delivered and bounced, so
  the rates read as what they are — over sends not all settled;
- the client's menu notes, edited on the Data tab under B1's settings
  contract (INT-2's GET …/settings): only the field, `expected_version` and
  `base`; a 409 refills the box with what is stored and keeps the admin's
  text to put back.

Source rules read from the template; behaviour run under node where it is
installed (the page's own functions over the payloads the server sends); a
render check over the Flask app for every read and the save the new code
makes — a renamed field fails here, not on Will's screen.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from flask import Flask

import admin_ops
import admin_routes
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "ui3-csrf"


@pytest.fixture(scope="module")
def page():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    """One top-level function's source, to the next top-level function or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet ", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def _const(src, name):
    m = re.search(r"\n(const " + re.escape(name) + r" = [^\n]*)", src)
    assert m, f"const {name} is gone from admin.html"
    return m.group(1)


# ── where each new field is read ─────────────────────────────────────────────

def test_issue_texts_show_on_push_and_alerts_and_on_the_clients_texts(page):
    p = _fn(page, "opsPush")
    assert "readErr(d, ['ops_issues'])" in p                       # the query label admin_ops gives the read
    assert "issueTextsHtml(d.issue_texts, itE, true)" in p          # the fleet: each row names its client
    m = _fn(page, "cMessages")
    sms = m[m.index("_cmsg === 'sms'"):m.index("_cmsg === 'push'")]
    assert "issueTextsHtml(d.issue_texts, readErr(d, ['ops_issues']), false)" in sms
    it = _fn(page, "issueTextsHtml")
    for key in ("it.failed_7d", "it.retrying", "it.not_texted_open", "it.recent", "r.notify_error",
                "r.notify_attempts", "r.notify_failed_at", "r.assignee_name", "var(--red-t)", "unknownV(err)"):
        assert key in it, key


def test_every_alert_listing_says_which_channels_it_went_out_on(page):
    """Every table of alert_log rows (keyed on fired_at) carries the channels
    column — a new listing without it fails here."""
    starts = [m.start() for m in re.finditer(r"key:\s*'fired_at'", page)]
    assert len(starts) >= 2, "the alert listings moved"
    for i in starts:
        assert "chanCell(r.channels)" in page[i:i + 700], page[i - 200:i + 200]
    assert "chanCell(r.channels)" in _fn(page, "opsPush") and "chanCell(r.channels)" in _fn(page, "cMessages")


def test_email_delivery_shows_in_flight_apart_and_a_failed_window_is_unknown(page):
    e = _fn(page, "opsEmail")
    assert "readErr(d, ['email_delivery_stats'])" in e
    assert "deliveryRow('7 days', r7, rE)" in e and "deliveryRow('30 days', r30, rE)" in e
    assert "rateCell('Bounce rate · 7 days', r7, 'bounce', th.bounce_warn_pct, th.bounce_crit_pct, rE)" in e
    assert "r7.in_flight" in e                                     # the caption names this week's unsettled sends
    row = _fn(page, "deliveryRow")
    for key in ("w.accepted", "w.delivered", "w.in_flight", "w.bounces", "w.complaints", "w.failed",
                "'in flight'", "unknownV(err)"):
        assert key in row, key
    assert "unknownV(err)" in _fn(page, "rateCell")


def test_the_menu_notes_edit_reads_the_settings_and_saves_under_the_contract(page):
    assert "lazy('cd-menunotes', () => menuNotesHtml(c))" in _fn(page, "cData")
    assert 'class="w" id="cd-menunotes"' in _fn(page, "menuHtml")
    assert "href=\"/admin/client-settings/${c.id}\"" not in _fn(page, "menuHtml")   # edited here now
    rd = _fn(page, "menuNotesHtml")
    assert "'/admin/api/client/' + c.id + '/settings'" in rd and "d.save_url" in rd and "d.version" in rd
    assert 'maxlength="${MENU_NOTES_MAX}"' in rd
    assert re.search(r"const MENU_NOTES_MAX = 2000;", page)
    sv = _fn(page, "mnSave")
    # Only the field, the version it loaded at and the value it loaded (B1).
    assert "post(m.url, {menu_notes: v, expected_version: m.version, base: {menu_notes: m.base}})" in sv
    assert "d._status === 409" in sv and "d.current" in sv and "d.current_version" in sv
    assert "okRes(d)" in sv and "errText(d)" in sv and "the outcome is unknown" in sv
    ex = _fn(page, "menuExtract")
    assert "mnUse()" in ex and "Not saved." in ex


def test_the_new_write_controls_are_marked_w(page):
    src = "".join(_fn(page, n) for n in ("menuHtml", "menuNotesHtml", "mnSave", "menuExtract"))
    pat = re.compile(r"<(button|a|label)\b([^<>]*?)(?:on(?:click|change)=\"(?:mnSave|mnPutBack|mnUse|menuExtract)\(|"
                     r"href=\"/admin/client-settings/)")
    seen, bad = 0, []
    for m in pat.finditer(src):
        seen += 1
        cls = re.search(r'class="([^"]*)"', m.group(2))
        if not cls or not re.search(r"(^|\s)w(\s|$)", cls.group(1)):
            bad.append(src[m.start():m.start() + 90])
    assert seen >= 6, "the menu's write controls moved"
    assert not bad, "write controls not marked .w:\n" + "\n".join(bad)


# ── behaviour, run in node ───────────────────────────────────────────────────

def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    # The page's own formatting (toLocaleString, local dates) in a fixed
    # locale and zone, so "2,000" and "9/29/26" don't depend on the machine.
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", TZ="America/Chicago")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _js(page, fns, consts=()):
    """The page's own helpers, with ico() and the DOM stubbed."""
    head = "\n".join(_const(page, n) for n in ("esc", "fmtN", "clientLink") + tuple(consts))
    body = "".join(_fn(page, n) for n in ("unknownV", "readErr", "pill", "parseTs", "mdy", "fmtD", "fmtT", "fmtDT",
                                          "stripDiv", "okRes", "errText") + tuple(fns))
    return "function ico(n){ return '<i:' + n + '>'; }\n" + head + "\n" + body + "\n"


def test_channels_say_what_went_out_and_never_guess(page):
    out = _node(_js(page, ("chanCell",), ("CHAN_LABEL",))
                + "console.log(JSON.stringify([null, '', 'none', 'sms,email,push', 'push'].map(chanCell)));")
    unrecorded, blank, none, three, push = out
    assert "Not recorded" in unrecorded and unrecorded == blank and "chip" not in unrecorded
    assert 'class="chip warn"' in none and ">none<" in none                  # raised, reached nobody
    assert [re.sub(r"<[^>]+>", "", c) for c in re.findall(r"<span class=\"chip\"[^>]*>[^<]*</span>", three)] == \
        ["text", "email", "push"]
    assert "push log" in push                                               # push = queued, not delivered


IT = {"failed_7d": 1, "retrying": 2, "not_texted_open": 1, "last_failed_at": "2026-09-29T15:00:00Z",
      "recent": [{"id": 7, "restaurant_id": 5, "restaurant": "Simple EJ's", "title": "Walk-in at <45F>",
                  "assignee_name": "Jim", "status": "open", "notify_attempts": 3,
                  "notify_error": "Twilio 21610: unsubscribed", "notify_failed_at": "2026-09-29T15:00:00Z"}]}


def test_issue_texts_are_red_with_their_reason_and_unknown_when_unread(page):
    many = dict(IT, recent=[dict(IT["recent"][0], id=i, title=f"Issue {i}") for i in range(8)])
    payloads = {"fleet": [IT, None, True], "client": [IT, None, False],
                "failed": [None, "ops_issues: database is locked", True],
                "failed_with_rows": [IT, "ops_issues: database is locked", True],
                "old_db": [None, None, True], "quiet": [{"failed_7d": 0, "retrying": 0, "not_texted_open": 0,
                                                         "recent": []}, None, False],
                "many": [many, None, True]}
    out = _node(_js(page, ("issueTextsHtml",)) + "const P = " + json.dumps(payloads) + ";\n"
                + "const o = {}; Object.keys(P).forEach(k => { o[k] = issueTextsHtml(P[k][0], P[k][1], P[k][2]); });"
                + "console.log(JSON.stringify(o));")
    f = out["fleet"]
    assert '<span style="color:var(--red-t)">Twilio 21610: unsubscribed</span>' in f     # the reason, in red
    assert "Walk-in at &lt;45F&gt;" in f and "<45F>" not in f                           # escaped
    assert "Simple EJ&#39;s" in f and "#client/5" in f                                   # the fleet names the client
    assert "to Jim" in f and ">3</span> tries" in f and "9/29/26" in f                    # M/D/YY, never ISO
    assert "2026-09-29" not in f
    assert "owner told instead" in f
    assert 'class="pill bad"' in f and "1 given up · 7 days" in f
    assert 'class="pill warn"' in f and "2 retrying now" in f and "1 open, not texted" in f
    assert "#client/5" not in out["client"] and "Twilio 21610" in out["client"]         # its own page: no link
    for k in ("failed", "failed_with_rows"):                                              # a failed read: never "none"
        assert ">unknown<" in out[k] and "given up on" not in out[k] and "Twilio" not in out[k]
    assert "Not recorded on this database yet" in out["old_db"]
    q = out["quiet"]
    assert "No issue text has been given up on." in q and 'class="pill good"' in q and "var(--red-t)" not in q
    many_html = out["many"]
    assert many_html.count('class="li"') == 8 and "3 more given up" in many_html and "<details" in many_html


def test_in_flight_sits_apart_from_delivered_and_bounced(page):
    w = {"sent": 30, "accepted": 25, "failed": 3, "bounces": 2, "complaints": 1, "delivered": 18, "in_flight": 4,
         "bounce_rate": 8.0, "complaint_rate": 4.0, "enough": False}
    js = (_js(page, ("deliveryFig", "deliveryRow", "rateCell")) + "const W = " + json.dumps(w) + ";\n"
          + "console.log(JSON.stringify([deliveryRow('7 days', W, null), deliveryRow('30 days', null, "
            "'email_delivery_stats: database is locked'), deliveryRow('30 days', null, null), "
            "rateCell('Bounce rate · 7 days', null, 'bounce', 4, 8, 'email_delivery_stats: database is locked'), "
            "rateCell('Bounce rate · 7 days', W, 'bounce', 4, 8, null)]));")
    row, failed, missing, rate_failed, rate = _node(js)
    words = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r'<span(?: title="[^"]*")?><span class="n">.*?</span></span>', row)]
    assert words == ["25 accepted", "18 delivered", "4 in flight", "2 bounced", "1 complaint", "3 failed"]
    # accepted = delivered + in flight + bounced + complaints; failed is apart.
    assert 18 + 4 + 2 + 1 == 25
    assert "has not reported delivery, a bounce or a complaint yet" in row
    assert ">unknown<" in failed and "database is locked" in failed and ">unknown<" not in missing
    assert ">unknown<" in rate_failed and "0<small>%</small>" not in rate_failed
    assert "too few sends to judge" in rate                               # judged only with enough sends


def _mn_js(page):
    """The menu-notes edit over a stub DOM: need/post/api answer from P."""
    return (_js(page, ("menuNotesHtml", "mnCountText", "mnCount", "mnSay", "mnSave", "mnPutBack", "mnUse"),
                ("MENU_NOTES_MAX",))
            + "let _mn = null; const els = {}; const posts = []; const gets = [];\n"
            + "function el(id){ if(!els[id]) els[id] = {value:'', textContent:'', innerHTML:'', disabled:false, style:{}, focus(){}}; return els[id]; }\n"
            + "const $ = (id) => els[id] || null; function skel(){ return ''; }\n"
            + "let answers = []; async function need(p){ gets.push(p); return answers.shift(); }\n"
            + "async function api(p, o){ gets.push(p); return answers.shift(); }\n"
            + "async function post(u, b){ posts.push([u, JSON.parse(JSON.stringify(b))]); return answers.shift(); }\n"
            + "function draw(html){ ['mn-text','mn-save','mn-count','mn-status','mn-mine'].forEach(el);"
              " els['mn-text'].value = (html.match(/<textarea[^>]*>([\\s\\S]*?)<\\/textarea>/) || ['', ''])[1]"
              ".replace(/&#39;/g, \"'\").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'); }\n")


def test_menu_notes_send_only_the_field_and_a_conflict_keeps_the_admins_text(page):
    js = _mn_js(page) + """
(async () => {
  const o = {};
  answers.push({ok:true, _status:200, version:7, settings:{menu_notes:"Short rib pasta"}, save_url:'/admin/client-settings/5'});
  const html = await menuNotesHtml({id:5, name:"Simple EJ's"}); draw(html);
  o.read = gets.slice(); o.box = els['mn-text'].value; o.html = html;
  await mnSave(); o.unchanged = [posts.length, els['mn-status'].textContent];
  // Somebody changed them first: a 409 carries what is stored now.
  els['mn-text'].value = '  Short rib pasta, truffle fries  ';
  answers.push({ok:false, _status:409, conflict:true, fields:['menu_notes'], labels:{menu_notes:'Menu notes'},
                current:{menu_notes:'Brunch Sat/Sun'}, current_version:9, error:'Changed by somebody else'});
  await mnSave();
  o.first = posts[0]; o.conflict = {box: els['mn-text'].value, version: _mn.version, base: _mn.base,
                                    mine: els['mn-mine'].innerHTML, said: els['mn-status'].textContent,
                                    tone: els['mn-status'].style.color};
  await mnSave(); o.after_conflict = [posts.length, els['mn-status'].textContent];   // the refill is no edit
  mnPutBack(); o.put_back = [els['mn-text'].value, els['mn-mine'].innerHTML];
  // Saved on purpose over the newer version; the server stored it trimmed of tags.
  answers.push({ok:true, _status:200, version:10, saved:{menu_notes:'Short rib pasta, truffle fries'}, changed:['menu_notes']});
  els['mn-text'].value = 'Short rib pasta, <b>truffle fries</b>';
  await mnSave();
  o.second = posts[1]; o.saved = {box: els['mn-text'].value, version: _mn.version, said: els['mn-status'].textContent};
  // A conflict lost at the write itself (no `current`): read what is stored again.
  els['mn-text'].value = 'Late edit';
  answers.push({ok:false, _status:409, conflict:true, fields:[], current_version:11, error:'Saved by someone else a moment ago'});
  answers.push({ok:true, _status:200, version:12, settings:{menu_notes:'Their late edit'}, save_url:'/admin/client-settings/5'});
  await mnSave();
  o.stale = {read: gets[gets.length - 1], box: els['mn-text'].value, version: _mn.version};
  // Any other refusal says the server's own sentence.
  els['mn-text'].value = 'Another';
  answers.push({ok:false, _status:400, error:'Menu notes must be text.'});
  await mnSave(); o.refused = els['mn-status'].textContent;
  // An extraction goes into the box, cut to what the notes keep, and is not saved.
  els['mx-text'] = {value: 'x'.repeat(2500)}; const n = posts.length;
  mnUse(); o.used = [els['mn-text'].value.length, posts.length - n, els['mn-status'].textContent];
  console.log(JSON.stringify(o));
})();
"""
    o = _node(js)
    assert o["read"] == ["/admin/api/client/5/settings"] and o["box"] == "Short rib pasta"
    assert 'maxlength="2000"' in o["html"]
    assert o["unchanged"] == [0, "Nothing changed."]
    url, body = o["first"]
    assert url == "/admin/client-settings/5"
    assert body == {"menu_notes": "Short rib pasta, truffle fries", "expected_version": 7,
                    "base": {"menu_notes": "Short rib pasta"}}                      # only the field (B1)
    c = o["conflict"]
    assert c["box"] == "Brunch Sat/Sun" and c["version"] == 9 and c["base"] == "Brunch Sat/Sun"   # refilled
    assert "Short rib pasta, truffle fries" in c["mine"] and "mnPutBack()" in c["mine"]           # theirs kept apart
    assert "someone changed the menu notes for Simple EJ's" in c["said"] and c["tone"] == "var(--red-t)"
    assert o["after_conflict"] == [1, "Nothing changed."]
    assert o["put_back"] == ["Short rib pasta, truffle fries", ""]
    assert o["second"][1] == {"menu_notes": "Short rib pasta, <b>truffle fries</b>", "expected_version": 9,
                              "base": {"menu_notes": "Brunch Sat/Sun"}}
    s = o["saved"]
    assert s["box"] == "Short rib pasta, truffle fries" and s["version"] == 10
    assert s["said"].startswith("Saved for Simple EJ's — as the box now shows")        # what was stored, said
    assert o["stale"] == {"read": "/admin/api/client/5/settings", "box": "Their late edit", "version": 12}
    assert o["refused"] == "Menu notes must be text."
    assert o["used"][0] == 2000 and o["used"][1] == 0 and "Cut to 2,000 characters" in o["used"][2]


# ── the render check ─────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
            if getattr(mod, "DB_PATH", None) == models.DB_PATH and mod is not models:
                monkeypatch.setattr(mod, "DB_PATH", db_path)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(admin_ops, "get_conn", redirect, raising=False)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    import push, webhooks, guest_marketing, sales_audits, ops, platform_monitor, provider_health, ai_utils
    push.init_push(db_path)
    webhooks.init_webhooks(db_path)
    guest_marketing.init_guest_marketing(db_path)
    sales_audits.init_sales_audits(db_path=db_path)
    for init in (ops.init_ops, admin_ops.init_admin_ops, platform_monitor.init_platform_tables,
                 provider_health.init_provider_health, ai_utils.init_ai_ops):
        init(db_path)
    admin_ops.invalidate_fleet_cache()

    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@ui3.test", billing_status="internal"),
                             db_path=db_path)
    admin = create_user(home, "will", "will@ui3.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    support = create_user(home, "helper", "helper@ui3.test", "Helper-pass-2026", role="support", db_path=db_path)
    rid = create_restaurant(Restaurant(name="Simple EJ's Test", owner_email="owner@ui3.test"), db_path=db_path)
    owner = create_user(rid, "ej", "owner@ui3.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    update_restaurant(rid, {"billing_status": "active", "module_reviews": 1, "menu_notes": "Short rib pasta"},
                      db_path=db_path)
    conn = real(db_path)
    try:
        x = conn.execute
        # One issue text given up on (the owner was told), one backing off.
        x("INSERT INTO ops_issues (restaurant_id, kind, title, status, assignee_name, notify_attempts, notify_error, "
          "notify_failed_at) VALUES (?, 'equipment', 'Walk-in at 45F', 'open', 'Jim', 3, "
          "'Twilio 21610: unsubscribed', datetime('now','-2 hours'))", (rid,))
        x("INSERT INTO ops_issues (restaurant_id, kind, title, status, notify_attempts, notify_next_at) "
          "VALUES (?, 'staffing', 'Short a line cook', 'open', 1, datetime('now','+5 minutes'))", (rid,))
        x("INSERT INTO alert_log (restaurant_id, alert_type, fired_at, channels) VALUES (?, '1star', datetime('now'), "
          "'sms,push')", (rid,))
        for i, status in enumerate(("sent", "delayed", "delivered", "bounced", "failed")):
            x("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, status, sent_at) VALUES "
              "(?, 'weekly_digest', ?, 'Your week', ?, datetime('now','-1 hours'))", (rid, f"m{i}@ui3.test", status))
        conn.commit()
    finally:
        conn.close()

    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.jinja_env.filters["format_num"] = lambda v: v
    app.jinja_env.filters["format_date"] = lambda v: v
    from auth_routes import auth_bp
    for bp in (admin_routes.admin_bp, auth_bp):
        app.register_blueprint(bp)

    def signed_in(uid):
        c = app.test_client()
        # No step-up: a menu-notes save must not need the password again.
        c.set_cookie("session_token", create_session(uid, db_path=db_path))
        c.set_cookie("csrf_js", CSRF)
        return c
    yield {"c": signed_in(admin), "support": signed_in(support), "rid": rid, "db": db_path}
    admin_ops.invalidate_fleet_cache()


def test_the_console_page_renders_with_the_new_panels(world):
    r = world["c"].get("/admin")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for fn in ("function issueTextsHtml(", "function chanCell(", "function deliveryRow(", "async function menuNotesHtml(",
               "async function mnSave(", "function mnPutBack("):
        assert fn in html, fn
    assert "{{" not in html and "{%" not in html


def _has(obj, path):
    """A dotted path, where `[]` steps into a list's first item."""
    cur = obj
    for part in path.split("."):
        if part.endswith("[]"):
            cur = cur[part[:-2]]
            assert isinstance(cur, list) and cur, f"{path}: {part[:-2]} is an empty list here"
            cur = cur[0]
        else:
            assert isinstance(cur, dict) and part in cur, f"{path}: no '{part}'"
            cur = cur[part]
    return True


ISSUE_TEXT_KEYS = (["issue_texts." + k for k in ("failed_7d", "retrying", "not_texted_open", "last_failed_at")]
                   + ["issue_texts.recent[]." + k for k in ("id", "restaurant_id", "restaurant", "title", "assignee_name",
                                                            "status", "notify_attempts", "notify_error",
                                                            "notify_failed_at")])
READS = [
    ("/admin/api/notifications", ISSUE_TEXT_KEYS + ["alerts[].channels", "errors", "query_errors", "unavailable"]),
    ("/admin/api/client/{rid}", ISSUE_TEXT_KEYS + ["alerts[].channels", "query_errors", "client.name"]),
    ("/admin/api/emails", ["rates.7d." + k for k in ("accepted", "delivered", "in_flight", "bounces", "complaints",
                                                     "failed", "bounce_rate", "complaint_rate", "enough")]
     + ["rates.30d.in_flight", "rates.thresholds.min_sends", "errors"]),
    ("/admin/api/client/{rid}/settings", ["ok", "version", "settings.menu_notes", "save_url", "step_up_fields"]),
]


@pytest.mark.parametrize("path,fields", READS, ids=[p.split("?")[0] for p, _f in READS])
def test_every_read_the_new_panels_make_answers_with_the_fields_they_read(world, path, fields):
    url = path.format(rid=world["rid"])
    r = world["c"].get(url)
    assert r.status_code == 200, (url, r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    for f in fields:
        _has(body, f)


def test_the_reads_carry_what_the_panels_draw(world):
    c, rid = world["c"], world["rid"]
    nt = c.get("/admin/api/notifications").get_json()
    assert (nt["issue_texts"]["failed_7d"], nt["issue_texts"]["retrying"]) == (1, 1)
    assert nt["issue_texts"]["recent"][0]["notify_error"] == "Twilio 21610: unsubscribed"
    assert nt["alerts"][0]["channels"] == "sms,push"
    cl = c.get(f"/admin/api/client/{rid}").get_json()
    assert [r["title"] for r in cl["issue_texts"]["recent"]] == ["Walk-in at 45F"]
    assert cl["alerts"][0]["channels"] == "sms,push"
    wk = c.get("/admin/api/emails").get_json()["rates"]["7d"]
    # sent + delayed are in flight; accepted = delivered + in flight + bounced + complaints; failed apart.
    assert (wk["in_flight"], wk["delivered"], wk["bounces"], wk["accepted"], wk["failed"]) == (2, 1, 1, 4, 1)
    st = c.get(f"/admin/api/client/{rid}/settings").get_json()
    assert st["save_url"] == f"/admin/client-settings/{rid}" and "menu_notes" not in st["step_up_fields"]
    assert st["settings"]["menu_notes"] == "Short rib pasta"


def test_the_menu_notes_save_the_page_sends_and_its_conflict(world):
    c, rid = world["c"], world["rid"]
    st = c.get(f"/admin/api/client/{rid}/settings").get_json()
    post = lambda body: c.post(st["save_url"], json=body, headers={"X-CSRF": CSRF})  # noqa: E731
    # Exactly what mnSave sends — and no step-up for this field.
    r = post({"menu_notes": "Short rib pasta, truffle fries", "expected_version": st["version"],
              "base": {"menu_notes": st["settings"]["menu_notes"]}})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["saved"] == {"menu_notes": "Short rib pasta, truffle fries"} and d["changed"] == ["menu_notes"]
    # Somebody else changes them; the box's next save is refused with what is stored now.
    update_restaurant(rid, {"menu_notes": "Brunch Sat/Sun"}, db_path=world["db"])
    r = post({"menu_notes": "Mine", "expected_version": d["version"], "base": {"menu_notes": d["saved"]["menu_notes"]}})
    assert r.status_code == 409
    j = r.get_json()
    assert j["conflict"] and j["fields"] == ["menu_notes"] and j["current"] == {"menu_notes": "Brunch Sat/Sun"}
    assert j["current_version"] and j["error"]
    assert models.get_restaurant(rid).menu_notes == "Brunch Sat/Sun"                    # nothing reverted
    # Put back and saved on purpose, against the version the refill carried.
    r = post({"menu_notes": "Mine", "expected_version": j["current_version"], "base": j["current"]})
    assert r.status_code == 200 and r.get_json()["saved"]["menu_notes"] == "Mine"
    # The server keeps 2,000 characters and no tags — the box shows what it stored.
    v2 = r.get_json()["version"]
    r = post({"menu_notes": "<b>Bold</b> " + "x" * 2100, "expected_version": v2, "base": {"menu_notes": "Mine"}})
    assert r.status_code == 200 and len(r.get_json()["saved"]["menu_notes"]) == 2000
    assert "<b>" not in r.get_json()["saved"]["menu_notes"]


def test_a_support_login_reads_the_settings_masked_and_cannot_save(world):
    s, rid = world["support"], world["rid"]
    r = s.get(f"/admin/api/client/{rid}/settings")
    assert r.status_code == 200 and r.headers.get("X-Redacted") == "support"   # api() turns every .w off
    r = s.post(f"/admin/client-settings/{rid}", json={"menu_notes": "x"}, headers={"X-CSRF": CSRF})
    assert r.status_code == 403
