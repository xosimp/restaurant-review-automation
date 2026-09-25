"""Density fix round, agent C — the web surfaces (items 12, 13, 24, 36–40, 47, 49).

  #12  Ask keeps its structure (## headings, numbered lists), 15px body,
       figures in the number face, lands on the TOP of the answer, and the
       evidence under it is one line.
  #13  the staff portal leads with the next shift, "Asked of you" under it,
       off days on one line, per-shift actions in a menu, the three forms
       behind one disclosure, DS fonts and tokens; the schedule link no
       longer prints its date twice.
  #24  Account: a primary button only for the one open editor's save (and
       the overview's one fix).
  #36  Account overview: one sentence, one fix, the ring toned by score, Data
       health at the top of Integrations driving its health item, unused POS
       providers folded into "Switch POS".
  #37  helper text 13.5px ink3; long row explanations behind an "i".
  #38  AI memory, decisions and trust under "Automation, AI & memory"; the
       daily report config its own owner-only rail item; the level dial
       leads Notifications; Appearance removed; Support one card + FAQ.
  #39  the bell's badge counts urgent only (server `urgent`), a summary line,
       red urgent dots; What's new and Sign out in the user menu; the FAB
       halo stops after the first open.
  #40  #recs: check-ins first, then a three-tile record strip with "—" below
       the floors (rec_learning.summary totals).
  #47  Billing shows the measured figure only.
  #49  Ask expands to a 720px side sheet.
"""
import os
import re

import pytest
from flask import Flask

import auth
import client_api
import models
from client_api import client_bp
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


SRC = _read("dashboard.html")


def _panel():
    i = SRC.index('<div class="panel" id="panel-account"')
    return SRC[i:SRC.index('<div class="toast" id="toast"', i)]


def _section(name):
    p = _panel()
    i = p.index('id="acct-%s"' % name)
    return p[i:p.index("</section>", i)]


# ── #12 / #49 Ask ────────────────────────────────────────────────────────────

def test_ask_renders_headings_lists_and_numbers_and_reads_at_15px():
    md = SRC[SRC.index("function _askMd(t){"):SRC.index("var _askAnchor")]
    assert "ask-h" in md and "openList('ol')" in md and "openList('ul')" in md
    assert "replace(/^#{1,6}\\s+/gm,'')" not in SRC, "headings are no longer stripped"
    assert "'<span class=\"hb-num\">$1</span>'" in SRC[SRC.index("function _askInline"):]
    assert re.search(r"\.ask-b \.bb\{[^}]*font-size:15px", SRC)


def test_every_block_after_an_answer_keeps_the_answer_top_in_view():
    for fn in ("_appendAskCavnarEvidence", "_appendAskCavnarProposal", "_appendAskCavnarSuggestions",
               "_appendAskCavnarFeedback", "_appendAskCavnarMessage"):
        i = SRC.index("function %s(" % fn)
        body = SRC[i:SRC.index("\n}\n", i)]
        assert "_askScroll(messages)" in body, fn
        assert "messages.scrollTop = messages.scrollHeight" not in body, fn
    assert "_askAnchor = isUser ? null : wrap;" in SRC


def test_the_evidence_under_an_answer_is_one_line_with_the_warnings_behind_why():
    i = SRC.index("function _appendAskCavnarEvidence(")
    body = SRC[i:SRC.index("\n}\n", i)]
    assert "ask-eva-line" in body and "unverified \\u00b7 Why?" in body
    assert "drawer.hidden = true" in body


def test_ask_expands_to_a_720px_side_sheet():
    assert 'onclick="askToggleWide()"' in SRC
    assert re.search(r"\.ask-panel\.wide\{[^}]*width:720px", SRC)
    assert "localStorage.setItem('cavAskWide'" in SRC


# ── #39 header and bell ──────────────────────────────────────────────────────

def test_the_bell_counts_urgent_in_red_and_says_what_needs_you():
    assert ".notif-row.is-urgent .nd{background:var(--red)}" in SRC
    assert re.search(r"#notif-badge\{[^}]*background:var\(--red\)", SRC)
    bell = SRC[SRC.index("function summaryLine()"):SRC.index("function loadNotifications()")]
    assert "' need' + (open.length === 1 ? 's' : '') + ' you</b>: '" in bell
    assert "filterBar = summaryLine() + filterBar;" in SRC


def test_whats_new_and_sign_out_live_in_the_user_menu():
    head = SRC[SRC.index('<header class="hdr">'):SRC.index("</header>")]
    menu = head[head.index('id="user-menu"'):]
    assert "What's new" in menu[:600] and 'action="/logout"' in menu[:900]
    assert 'class="cbtn cbtn-secondary cbtn-sm logout-btn">Sign out' not in head
    assert head.count('action="/logout"') == 1


def test_the_ask_fab_halo_stops_after_the_first_open():
    assert ".ask-fab.seen:before{animation:none}" in SRC.replace(".ask-fab.open:before,", "")
    assert "sessionStorage.setItem('cavAskSeen', '1')" in SRC


@pytest.fixture
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return db_path


def test_the_unread_count_carries_the_urgent_unresolved_count(_db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Bell Co", owner_email="o@x.test"), db_path=_db)
    conn = models.get_conn(_db)
    for t in ("health", "labor_over"):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?,?)", (rid, t))
    conn.commit()
    conn.close()
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "is_admin": 0, "role": "owner",
            "username": "owner", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_bp)
    body = app.test_client().get("/api/notifications/unread-count").get_json()
    items = client_api._do_get_notifications(rid, viewer=user)[0]["notifications"]
    want = sum(1 for n in items if n["urgent"] and not n["resolved"])
    assert body["count"] == 2 and body["urgent"] == want and want >= 1


# ── #40 #recs ────────────────────────────────────────────────────────────────

def test_recs_puts_the_check_ins_first_then_the_record_strip():
    r = SRC[SRC.index("  function render(g){\n    var b=body();"):SRC.index("  function worked(w){")]
    assert r.index("recCheckinCandidates(") < r.index("strip(g.summary)+worked(g.worked)+followed(g.summary)")
    strip = SRC[SRC.index("  function strip(s){"):SRC.index("  function worked(w){")]
    assert "t.taken_enough?" in strip and "t.measured_enough?" in strip and ":'—'" in strip


def test_the_summary_totals_carry_their_own_floors(monkeypatch):
    import rec_learning

    class _C:
        def close(self):
            pass

    def ep(i, state, verdict=None):
        return {"rec_id": i, "shown": True, "state": state, "module": "labor", "verdict": verdict,
                "tracker": {}, "tag_list": []}
    eps = ([ep(1, "accepted", "improved"), ep(2, "accepted", "improved"), ep(3, "completed", "improved"),
            ep(4, "accepted", "worsened"), ep(5, "accepted"), ep(6, "accepted")]
           + [ep(10 + i, "dismissed") for i in range(4)] + [ep(20, "ignored"), ep(21, "ignored")]
           + [ep(30, "open"), ep(31, "open")])
    monkeypatch.setattr(rec_learning, "get_conn", lambda *a, **k: _C())
    monkeypatch.setattr(rec_learning, "_load", lambda *a, **k: [dict(e) for e in eps])
    t = rec_learning.summary(1, days=90)["totals"]
    assert (t["shown"], t["settled"], t["taken"], t["open"]) == (14, 12, 6, 2)
    assert t["taken_enough"] is True
    assert (t["measured"], t["improved"], t["measured_enough"]) == (4, 3, False)


# ── #24 / #36 / #37 / #38 / #47 Account ─────────────────────────────────────

def test_account_keeps_a_primary_only_for_the_open_editors_save_and_the_one_fix():
    tags = re.findall(r"<[^>]*cbtn-primary[^>]*>", _panel())
    assert len(tags) == 2 and any('id="pe-save"' in t for t in tags) and any('id="ac-fix-btn"' in t for t in tags)


def test_the_overview_is_one_sentence_one_fix_and_a_toned_ring():
    ov = _section("overview")
    assert 'id="ac-health-say"' in ov and 'id="ac-fix-btn"' in ov and 'class="ac-more"' in ov
    for gone in ("Google {{ 'connected'", "POS {{ 'needs attention'", "2FA on", 'id="ac-sub-chip"'):
        assert gone not in ov, gone
    js = SRC[SRC.index("window.acctHealthRefresh=function(){"):SRC.index("window.acctApplyBilling=")]
    assert "score>=90?'good':(score>=60?'warn':'bad')" in js and "sayAndFix(score)" in js
    assert ".ac-ring.good .ar{stroke:var(--green)" in SRC and ".ac-ring.bad .ar{stroke:var(--red)" in SRC


def test_data_health_leads_integrations_and_drives_its_health_item():
    it = _section("integrations")
    assert it.index('id="acct-dh-card"') < it.index("{{ pos_card(")
    assert 'data-dh-open="1"' in it
    js = SRC[SRC.index("window.acctHealthRefresh=function(){"):SRC.index("window.acctApplyBilling=")]
    assert "dh&&dh.pct!=null" in js
    assert "cavDataHealth.fetch(false).then(acctApplyDataHealth)" in SRC


def test_unused_pos_providers_fold_into_switch_pos():
    it = _section("integrations")
    assert it.count("{{ pos_card(") == 4
    assert it.count('<div class="ac-pos-other" hidden>') == 4
    assert "<b>Switch POS</b>" in it and 'onclick="acctPosOthers(this)"' in it


def test_helper_text_is_quieter_and_long_row_explanations_sit_behind_an_i():
    for sel in (".ac-h p{", ".ac-card-h p{", ".ac-row .l span{"):
        rule = SRC[SRC.index(sel):SRC.index("}", SRC.index(sel))]
        assert "font-size:13.5px" in rule and "color:var(--ink3)" in rule, sel
    q = SRC[SRC.index("function acctQuietHelp(){"):SRC.index("// ── Opening the tab")]
    assert "if(words<=8)continue;" in q and "aria-controls" in q and "sp.hidden=true" in q


def test_account_filing():
    p = _panel()
    auto, sec = _section("automation"), _section("security")
    for card in ('id="acct-memory-card"', 'id="acct-decisions-card"', 'id="acct-trust-card"'):
        assert card in auto and card not in sec, card
    assert "Automation, AI &amp; memory" in auto
    # The daily report config: its own owner-only rail item and section.
    rep = _section("report")
    assert 'id="as-dsr-card"' in rep and 'id="as-dsr-card"' not in _section("restaurant")
    i = p.index('data-go="report"')
    assert "{% if _is_owner %}" in p[i - 200:i]
    # The level dial leads Notifications.
    notif = _section("notifications")
    assert notif.index('id="as-level-card"') < notif.index('class="ac-grid"')
    assert 'id="as-briefing-level"' in notif[:notif.index('class="ac-grid"')]
    # Appearance is gone, from the rail, the page, the section list and the palette.
    assert "acct-appearance" not in SRC and 'data-go="appearance"' not in SRC and "'appearance'" not in SRC
    import command_center
    assert "appearance" not in [s[0] for s in command_center._ACCOUNT_SECTIONS]
    assert "report" in [s[0] for s in command_center._ACCOUNT_SECTIONS]
    # Support: one card, the FAQ behind a link.
    sup = _section("support")
    assert sup.count('class="ac-card') == 1
    assert "aria-controls=\"ac-faq-fold\"" in sup and 'id="faq-list"' in sup


def test_billing_shows_the_measured_figure_only():
    assert 'id="billing-value"' in _section("billing")
    fn = SRC[SRC.index("window.acctBillingValue=function(){"):SRC.index("window.acctPosOthers=")]
    assert "d.net_monthly" in fn and "Measured, net:" in fn and "Nothing measured yet" in fn
    for other in ("opportunity", "surfaced", "avoided"):
        assert other + "." not in fn and "v." + other not in fn, other


# ── #13 staff portal ─────────────────────────────────────────────────────────

def test_the_staff_portal_leads_with_the_next_shift_and_what_is_asked_of_you():
    s = _read("staff_portal.html")
    tab = s[s.index('<section id="tab-schedule"'):s.index("</section>", s.index('<section id="tab-schedule"'))]
    assert tab.index('id="next-shift"') < tab.index('id="asks-body"') < tab.index('id="sched-body"')
    disc = tab[tab.index('id="disc-body"'):]
    for body in ('id="avail-body"', 'id="pref-body"', 'id="timeoff-body"'):
        assert body in disc, body
    assert 'class="card"' not in s and "today-line" not in s
    assert "/static/fonts/cavnar-fonts.css" in s and "Space+Grotesk" in s and "'Apfel Grotezk'" in s
    render = s[s.index("function renderShifts(d){"):s.index("fetch('/staff/api/shifts')")]
    assert "offs.push(" in render and "'<div class=\"offline\">Off <b>'" in render
    assert "more-btn" in render and 'class="shift-menu drop"' in render
    assert "ahtml += '<h2>Asked of you</h2>';" in s and "hero.asks = asks.length" in s


def test_the_schedule_link_prints_the_date_once():
    s = _read("staff_schedule.html")
    assert "{{ s.day or s.date_label }}" not in s
    assert '{% if s.day %}<span class="day">{{ s.day }}</span>{% endif %}<span class="date">{{ s.date_label }}</span>' in s
