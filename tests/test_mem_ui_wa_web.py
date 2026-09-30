"""Memory round, UI wave — web owner dashboard, part A (UI-WA), pinned in
templates/dashboard.html: each new element against the payload field it
reads, so a renamed field fails here and not on the owner's screen.

Two halves, as in the admin round's tests/test_fix_ui_ui*.py:

- Source rules, read from the template: Home's cards, Needs attention, the
  one thing, the brief, the kind holds and the quieter line; the daily
  report's priorities, blocks and Tomorrow; Ask; Account's memory, goals,
  targets, notifications, change history and trust; Data Health; the undo
  question; the policy notice; the decline that reads "Not for us".
- Behaviour under node: the shared pieces (what was said before, the
  caution, the conflict chooser, the answer row's own labels, the undo
  question) and the Why? panel's prior group and profile unlock, run on
  payloads shaped like the server's.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name, src=SRC):
    """One function's source, from its declaration to the next function."""
    m = re.search(r"\n\s*(?:window\.)?" + re.escape(name) + r"\s*=\s*function\s*\(|\n\s*function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"{name}() is gone from dashboard.html"
    nxt = re.search(r"\n\s*(?:function\s+\w+\s*\(|window\.\w+\s*=\s*function)", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def _between(start, end, src=SRC):
    i = src.index(start)
    return src[i:src.index(end, i + len(start))]


# ── the decline reads "Not for us", everywhere ──────────────────────────────

def test_the_decline_is_not_for_us_on_every_answer_row():
    assert ">Pass<" not in SRC and 'aria-label="Pass"' not in SRC and 'title="Pass"' not in SRC
    for name in ("hbRecCard", "renderFocus", "recNotForUsHtml"):
        assert ">Not for us</button>" in _fn(name), name
    assert "recEsc(lb.not_for_us||'Not for us')" in _fn("recControlsHtml")


def test_home_says_what_the_answer_does_in_the_servers_words():
    hd = SRC[SRC.index("window.hbDismiss=function("):SRC.index("  // ── answer in place")]
    assert "d.message?String(d.message)" in hd
    assert "else msg=d.message?String(d.message):" in hd          # a Done with no tracker
    assert "if(d&&d.message)return String(d.message);" in _fn("_recAnsweredText")


# ── Home: cards, Needs attention, the one thing ──────────────────────────────

def test_a_card_carries_its_caution_what_was_said_before_its_retest_and_its_conflict():
    card = _fn("hbRecCard")
    assert "r.caution&&window.recCautionHtml?recCautionHtml(r.caution)" in card
    assert "window.recPrevHtml?recPrevHtml(r)" in card
    assert "if(r.retest)meta.push('<span class=\"rt\">Back for a re-test</span>');" in card
    assert "r.conflict&&window.recConflictHtml?recConflictHtml(r.conflict,'home')" in card
    # the conflict sits on the card, never behind Details
    assert card.index("recConflictHtml(") < card.index("<details class=\"hb-rec-more\">")


def test_needs_attention_rows_name_the_earlier_answer():
    assert "(window.recPrevHtml?recPrevHtml(a):'')" in _fn("renderAttention")


def test_the_one_thing_carries_its_leads_caution_answers_and_conflict():
    f = _fn("renderFocus")
    for lead in ("lo=ff;", "lo=att[0];", "lo=recs[0];"):
        assert lead in f
    assert "lo.caution&&window.recCautionHtml" in f and "recPrevHtml(lo)" in f
    assert "lo.conflict&&window.recConflictHtml)h+=recConflictHtml(lo.conflict,'home')" in f


def test_quieter_names_each_kinds_retest_date():
    recs = _fn("renderRecs")
    assert "q.review_on?' (back for a re-test on '+q.review_on+')'" in recs
    assert "h+=hbKindHolds(d.kind_holds||[]);" in recs
    # an empty grid still asks the question (every card may have been held)
    assert "Nothing to recommend yet — that changes as your data grows.</div>'+hbKindHolds(d.kind_holds||[])" in recs


def test_a_kind_hold_is_a_question_with_its_record_and_its_own_answers():
    kh = _fn("hbKindHolds")
    for field in ("kh[i].rec_key", "k.measured", "k.improved", "k.worsened", "k.do_nothing_pct", "k.title", "k.why",
                  "k.answerable!==false", "labels:k.answers||{}"):
        assert field in kh, field
    assert "noWhy:1" in kh and "noTrack:1" in kh
    assert "String(key).indexOf('kind_hold:')" in kh                  # answered: the card leaves
    assert '<i class="up"' in kh and '<i class="dn"' in kh and "<b style=\"left:" in kh
    assert ".hb-hold-bar b{" in SRC and ".hb-hold-bar i.up{" in SRC


def test_the_answer_row_takes_a_questions_own_words_and_skips_the_picker():
    ctl = _fn("recControlsHtml")
    assert "var lb=(opts&&opts.labels)||{};" in ctl and "data-rec-nowhy=\"1\"" in ctl
    assert "&&!t.getAttribute('data-rec-nowhy')){recAskWhy(t);return;}" in SRC


def test_the_labor_tile_says_what_its_delta_is_against():
    assert "(l.delta.label?'<small class=\"dlb\">'+esc(l.delta.label)+'</small>':'')" in _fn("renderSignals")


# ── Home: the brief ─────────────────────────────────────────────────────────

def test_the_reports_own_priority_is_left_to_the_report():
    follow = _fn("renderFollow")
    assert "return !(l.key==='dsr_action'&&l.source==='dsr');" in follow
    assert "+num(l.text)+hbBriefExtra(l)+acts+" in follow


def test_the_reports_today_line_lists_its_calls_and_a_line_can_carry_a_conflict():
    ex = _fn("hbBriefExtra")
    assert "(l.source==='dsr'&&l.key==='today')?(l.predictions||[]):[]" in ex
    assert "The report’s calls" in ex
    assert "l.conflict&&window.recConflictHtml)h+=recConflictHtml(l.conflict,'home')" in ex


# ── the daily report ────────────────────────────────────────────────────────

def test_a_priority_carries_its_caution_and_conflict():
    n = _fn("narrativeHtml")
    assert "x.caution&&window.recCautionHtml?recCautionHtml(x.caution)" in n
    assert "x.conflict&&!x.answered&&window.recConflictHtml?recConflictHtml(x.conflict,'dsr')" in n


def test_a_priority_left_out_for_what_the_rest_knows_is_listed_with_its_reason():
    v = _fn("verifyHtml")
    assert "else if(dd.key&&dd.why)left.push(dd);" in v
    assert "if(isNum(vf.failed_check))dropped=vf.failed_check;" in v
    assert "'<span>Left out: '+prose(left[li].why)+'.</span></div>'" in v


def test_sales_says_the_owners_goal_never_a_budget_they_did_not_enter():
    sales = _between("    sales:function(b,p){", "    labor:function(b){")
    assert "var bg=d.budget||{},isGoal=bg.source==='goal';" in sales
    assert "cmp(isGoal?'vs your goal':'vs budget'" in sales
    assert "isGoal&&bg.label?'<div class=\"subl\">'+prose(bg.label)+'</div>'" in sales


def test_the_forecast_says_its_base_its_effects_or_why_there_is_none():
    sales = _between("    sales:function(b,p){", "    labor:function(b){")
    assert "isNum(fb.base_net)&&(fb.effects||[]).length" in sales and "drEffects(fb.effects)" in sales
    assert "else if(bl.forecast&&bl.forecast.reason)h+=note('No forecast to compare with: '" in sales
    eff = _fn("drEffects")
    for field in ("e.lift_pct", "e.display", "e.label", "e.n"):
        assert field in eff
    tm = _fn("tomorrowHtml")
    assert "(fc.effects||[]).length?drEffects(fc.effects)" in tm and "isNum(fc.base)" in tm


def test_the_weather_that_happened_sits_beside_the_forecast():
    intel = _between("    intel:function(b){", "    closeout:function(b){")
    assert "var ob=w.observed||{};" in intel
    assert "if(ob.summary)t+=tile('Weather (actual)'" in intel
    assert "ob.basis" in intel and "if(w.note&&!ob.summary)" in intel


def test_the_prime_cost_goal_names_its_date():
    assert "k.target.source==='goal'?prose(k.target.label)+': '" in _fn("kpiTile")


# ── Ask ─────────────────────────────────────────────────────────────────────

def test_ask_counts_the_advice_it_repeats_that_the_owner_passed_on():
    ev = _fn("_appendAskCavnarEvidence")
    assert "var reps = d.declined_repeats || [];" in ev and "reps[ri].declined_on" in ev
    assert "you passed on before" in ev and ".ask-eva-rep{" in SRC


def test_a_rating_that_became_a_preference_is_said_once():
    assert "_askPreferenceToast(d.preference);" in _fn("_askFeedbackPost")
    t = _fn("_askPreferenceToast")
    assert "Got it \\u2014 shorter answers for you (Account \\u2192 Memory)" in t
    assert "p.preference" in t and "localStorage" in t and "try {" in t


def test_a_teammates_goal_is_sent_to_the_owner():
    run = _fn("_runAskCavnarProposal")
    assert "if (d.proposed === true) {" in run and "Sent to the owner to confirm" in run


def test_new_confirm_cards_and_module_chips_render_through_the_generic_pieces():
    card = _fn("cavPropCard")
    assert "var shown = p.fields_shown || [];" in card and "fdd.textContent = shown[f].value;" in card
    # the chips are the server's labels ("past chats", "earlier reads", ...), drawn as sent
    assert "ms.textContent = mods.join(' \\u00b7 ');" in _fn("_appendAskCavnarEvidence")


# ── Account ─────────────────────────────────────────────────────────────────

def test_memory_shows_who_what_about_whom_until_when_and_what_left():
    meta = _fn("memMeta")
    for field in ("f.kind", "f.modules", "f.author", "f.created_on", "f.due_label", "f.valid_until_label", "f.audience"):
        assert field in meta, field
    assert "a === 'principals' ? 'Only owners' : (a === 'author' ? 'Just you'" in _fn("memAudience")
    lm = _fn("loadMemory")
    assert "memLanes(d.lanes);" in lm and "f.can_forget === false" in lm
    assert "d.archived" in lm
    # The archive row moved into memArchRow when the archive became paged
    # (memory re-audit 9/29/26, R3 archive_restore).
    row = _fn("memArchRow")
    for field in ("x.reason_label", "x.archived_on", "x.can_restore", "data-restore-fact"):
        assert field in row, field
    lanes = _fn("memLanes")
    assert "l.cap" in lanes and "l.count" in lanes
    assert "fetch('/api/account/memory/restore'" in SRC and "JSON.stringify({id: +b.getAttribute('data-restore-fact')})" in SRC


def test_the_add_form_sends_kind_modules_dates_and_audience():
    rem = _fn("acctRemember")
    for key in ("body.modules = mods", "body.audience = aud", "body.due_on = due", "body.valid_until = until",
                "{fact: fact, kind: kind}"):
        assert key in rem, key
    form = _between('<div class="ac-mem-opts" id="acct-memory-opts">', '<div id="acct-memory-arch" hidden>')
    assert 'type="date" id="acct-memory-until"' in form and 'type="date" id="acct-memory-due"' in form
    assert 'type="time"' not in form
    assert '<option value="principals">Only owners</option>' in form and '<option value="author">Just you</option>' in form
    for mod in ("labor", "food", "reviews", "marketing", "intel", "ops"):
        assert f'data-mem-mod="{mod}"' in form


def test_goals_proposed_by_a_teammate_wait_for_the_owner():
    g = _fn("loadGoalsCard")
    for field in ("d.proposed", "p.proposed_by", "d.can_confirm", "p.summary", "d.goals"):
        assert field in g, field
    assert "fetch('/api/goals/' + encodeURIComponent(id) + '/' + ans" in SRC
    assert 'data-goal-answer="confirm"' in g and 'data-goal-answer="decline"' in g
    assert "loadGoalsCard();" in _fn("loadPause")


def test_targets_say_the_goal_that_applies_and_who_set_them():
    n = _fn("tgNotes")
    assert "goals = {labor_target_pct: (t.labor || {}).goal, food_cost_target: (t.food || {}).goal}" in n
    assert "g.label + ' applies'" in n and "t.set_notes" in n
    assert "tgNotes(t);" in _fn("tgRender") and "tgNotes(d.targets);" in SRC


def test_notifications_just_for_me_saves_each_change_and_never_uses_a_time_input():
    r = _fn("mineRender")
    for field in ("m.push_enabled", "m.morning_brief", "m.quiet_start", "m.quiet_end", "m.push_muted_types",
                  "d.alert_types"):
        assert field in r, field
    card = _between('<div class="ac-card" id="as-mine-card" hidden>', '<div class="ac-card span" id="as-org-card" hidden>')
    assert 'type="time"' not in card and 'id="as-mine-qs"' in card and 'id="as-mine-qe"' in card
    assert "cavSetTime(document.getElementById('as-mine-qs')" in r
    assert "fetch('/api/account/preferences/mine'" in _fn("mineSave")
    # a mute chip saves the moment it is tapped, as a change to the stored list
    assert "mineSave({push_muted_types: mineMutedNow(ty, on)});" in SRC
    lm = _fn("loadMine")
    assert "fetch('/api/account/preferences'" in lm and "fetch('/api/notifications/engagement'" in lm
    assert "(e && e.ok && e.mine)" in lm and "mute them just for you?" in lm


def test_where_each_setting_comes_from_and_every_location():
    o = _fn("orgRender")
    assert "loc[k].source" in o and "d.can_apply_to_all" in o and "data-org-apply" in o
    assert "'all locations': 'All locations', 'this location': 'This location'" in SRC
    assert "fetch('/api/account/preferences/apply-to-all'" in SRC and "JSON.stringify({keys: keys})" in SRC


def test_change_history_reads_each_changes_own_line():
    sec = _fn("loadSecurityExtras")
    assert "var ch=(d.ok&&d.changes)||[];" in sec and "c.line" in sec
    assert "onclick=\"acctLogOpen('change-log','Change history','Settings')\"" in SRC


def test_trust_says_when_a_band_earned_it_what_lapsed_and_what_was_undone():
    t = _fn("loadTrust")
    for field in ("b.earned_at", "b.autoposted_clean", "d.lapsed", "lp[i].text", "sc.undone_on", "s.undone_at",
                  "s.clean_since_undo"):
        assert field in t, field


def test_a_teammates_voice_option_says_what_it_does():
    assert "var ACCESS_HELP={'reviews.voice':'Their approved replies teach Cavnar AI your voice.'};" in SRC


# ── Data Health, the undo question, the Why? panel ──────────────────────────

def test_data_the_owner_distrusts_is_listed_with_re_verified():
    d = _fn("dhDistrustHtml")
    for field in ("x.source", "x.label", "x.since", "x.text", "x.verify&&x.verify.web"):
        assert field in d, field
    assert ">Re-verified</button>" in d
    v = _fn("dhVerify")
    assert "JSON.stringify({source:src})" in v and "r.message" in v
    assert "h+=dhDistrustHtml(d.distrusted);" in _fn("dhBody")
    assert "cavDataHealth.distrust(d.distrusted)" in SRC and 'id="acct-dh-dist"' in SRC


def test_an_undone_automatic_send_asks_why_once():
    u = _fn("cavUndoWhyHtml")
    assert "aw.route" in u and "aw.options" in u and "data-undo-why=" in u
    assert "'/api'+String(aw.route)" in u
    assert "JSON.stringify({reason_code:b.getAttribute('data-undo-why')})" in SRC
    assert "cavUndoWhyHtml(d.ask_why)" in SRC and "n._askWhy = d.ask_why || null;" in SRC
    assert "cavUndoWhyHtml(n._askWhy)" in _fn("renderList")


def test_the_why_panel_names_the_prior_group_and_the_profile_unlock():
    conf = re.search(r'<script id="cav-conf">(.*?)</script>', SRC, re.S).group(1)
    assert "a.prior_unlock==='confirm_profile'" in conf and 'data-bm-profile="1"' in conf
    assert "str((obj(a.prior)||{}).rung)||str(a.prior_rung)" in conf and "str(a.cohort_label)" in conf
    # the benchmark link it reuses closes the explanation first
    assert "var em=document.getElementById('explain-modal');if(em&&em.classList)em.classList.remove('show');" in _fn("openProfile")


# ── the policy notice ───────────────────────────────────────────────────────

def test_the_policy_notice_reads_the_payload_and_dismisses_for_this_login():
    p = _fn("renderPolicyNotice")
    for field in ("d.policy_notice", "p.text", "p.url", "p.link_label", "p.dismiss&&p.dismiss.web"):
        assert field in p, field
    assert "/^https:\\/\\//.test(" in p and 'rel="noopener"' in p and 'data-notice-dismiss="1"' in p
    assert "h+=renderPolicyNotice(d);" in _fn("render") and "h+=renderPolicyNotice(g);" in _fn("renderGroup")
    assert "fetch(card.getAttribute('data-notice-route')" in SRC


def test_the_marketing_feed_draws_a_conflict():
    assert "o.conflict && window.recConflictHtml ? recConflictHtml(o.conflict, 'marketing')" in _fn("mktOppCard")


# ── behaviour under node ────────────────────────────────────────────────────

def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _shared_pieces():
    i = SRC.index("function recEsc(v){")
    return SRC[i:SRC.index("/* The server says what the answer actually did", i)]


def _run_shared(expr, **fixtures):
    decl = "".join(f"var {k}={json.dumps(v)};\n" for k, v in fixtures.items())
    js = ("var window={};var document={addEventListener:function(){}};\n" + _shared_pieces() + "\n" + decl
          + f"console.log(JSON.stringify({expr}));")
    return _node(js)


def test_what_was_said_before_renders_both_answers_and_nothing_without_them():
    x = {"previous_answer": {"answered_on": "3/12/26", "text": "You passed on this on 3/12/26 ($120/mo then)."},
         "delegate_answer": {"by": "Dana", "text": "Dana passed on this: already doing it (9/28/26)"}}
    h = _run_shared("recPrevHtml(x)", x=x)
    assert 'class="rec-prev"' in h and 'class="rec-prev dlg"' in h
    assert "You passed on this on 3/12/26" in h and "Dana passed on this" in h
    assert _run_shared("recPrevHtml({})") == "" and _run_shared("recPrevHtml(null)") == ""


def test_the_conflict_chooser_posts_each_options_signature():
    cf = {"id": "trim_vs_fill:labor:day:tuesday", "rule": "trim_vs_fill",
          "label": "Trimming a day you are also trying to fill", "with": "Run a Tuesday promotion",
          "why": "Tuesday: one card trims staffing, another tries to bring guests in — they pull against each other.",
          "choose": [{"signature": "labor:day:tuesday", "key": "trim_day:Tuesday", "label": "Trim Tuesday"},
                     {"signature": "marketing:day:tuesday", "key": "slow_day:tue", "label": "Fill Tuesday"}],
          "route": {"web": "/api/recs/conflict", "mobile": "/mobile/api/recs/conflict"}}
    h = _run_shared("recConflictHtml(cf,'home')", cf=cf)
    assert 'data-cf-id="trim_vs_fill:labor:day:tuesday"' in h and 'data-cf-route="/api/recs/conflict"' in h
    assert 'data-cf-prefer="labor:day:tuesday"' in h and 'data-cf-prefer="marketing:day:tuesday"' in h
    assert "Run a Tuesday promotion" in h and "Which should Cavnar AI keep?" in h and ">or<" in h
    assert 'class="cbtn cbtn-secondary cbtn-sm"' in h
    assert _run_shared("recConflictHtml({id:'x',choose:[]})") == ""
    hold = dict(cf, id="reprice_vs_value:pricing:dish:carbonara", choose=[
        {"signature": "pricing:dish:carbonara", "key": "reprice:carbonara", "label": "Keep it"},
        {"signature": "hold", "key": None, "label": "Hold it"}])
    h = _run_shared("recConflictHtml(cf,'home')", cf=hold)
    assert 'data-cf-prefer="hold"' in h and ">Hold it<" in h


def test_a_caution_is_one_amber_line_and_none_is_nothing():
    h = _run_shared("recCautionHtml(c)", c="Before cutting: 3 service complaints on Tuesday nights.")
    assert h.startswith('<div class="rec-caution" role="note">') and "Before cutting" in h
    assert _run_shared("recCautionHtml('')") == ""


def test_a_kind_hold_answer_row_uses_its_own_words_without_the_picker():
    h = _run_shared("recControlsHtml('kind_hold:trim_day','home','home',{noTrack:1,noWhy:1,labels:{completed:'Keep suggesting it',not_for_us:'Stop suggesting it'}})")
    assert ">Keep suggesting it<" in h and ">Stop suggesting it<" in h and 'data-rec-nowhy="1"' in h
    assert "Measure it" not in h
    plain = _run_shared("recControlsHtml('trim_day:Tuesday','home','labor')")
    assert ">Done<" in plain and ">Not for us<" in plain and "data-rec-nowhy" not in plain


def test_the_undo_question_uses_the_servers_options_and_route():
    aw = {"route": "/actions/12/why", "options": [{"code": "not_ready", "label": "It wasn't ready yet"},
                                                   {"code": "bad_timing", "label": "Bad timing"}]}
    h = _run_shared("cavUndoWhyHtml(aw)", aw=aw)
    assert 'data-undo-why-route="/api/actions/12/why"' in h
    assert 'data-undo-why="not_ready"' in h and "It wasn&#39;t ready yet" in h and 'data-undo-why-skip="1"' in h
    assert _run_shared("cavUndoWhyHtml(null)") == ""


def _conf(expr, **fixtures):
    block = re.search(r'<script id="cav-conf">(.*?)</script>', SRC, re.S).group(1)
    decl = "".join(f"var {k}={json.dumps(v)};\n" for k, v in fixtures.items())
    return _node("var window={};\n" + block + "\nvar C=window.cavConf;\n" + decl
                 + f"console.log(JSON.stringify({expr}));")


BASE = {"pct": 58, "band": "medium", "label": "58% confidence", "reason": "Not enough history yet", "version": 2,
        "dimensions": {"evidence": {"pct": 70, "basis": "6 Tuesdays in your shift data", "n": 6},
                       "freshness": {"pct": 90, "basis": "Shifts through 9/27/26", "as_of_iso": "2026-09-27"}}}


def test_an_unconfirmed_profile_offers_the_step_that_unlocks_a_finer_group():
    c = json.loads(json.dumps(BASE))
    c["dimensions"]["accuracy"] = {"pct": None, "basis": "Not enough history yet — 2 measured, needs 5", "n": 2,
                                   "source": "none", "prior_rung": None, "prior_unlock": "confirm_profile"}
    panel = _conf("C.panel(C.norm(c))", c=c)
    assert 'data-bm-profile="1"' in panel and "Confirm your restaurant profile" in panel
    assert "to compare with restaurants like yours" in panel
    c["dimensions"]["accuracy"]["prior_unlock"] = None
    assert "data-bm-profile" not in _conf("C.panel(C.norm(c))", c=c)


def test_the_prior_group_is_named_from_the_rung_or_the_servers_label():
    c = json.loads(json.dumps(BASE))
    c["dimensions"]["accuracy"] = {"pct": 71, "basis": "improved 4 of 6 times vs about 30% by chance", "n": 6,
                                   "improved": 4, "source": "own", "low": 30, "high": 90,
                                   "prior": {"source": "do_nothing", "rung": "partition"}, "cohort_label": None}
    rows = _conf("C.rows(C.norm(c))", c=c)
    acc = [r for r in rows if r["key"] == "accuracy"][0]
    assert "prior from restaurants like yours" in acc["detail"]
    c["dimensions"]["accuracy"] = {"pct": None, "basis": "Not enough history yet — 3 measured, needs 5", "n": 3,
                                   "source": "cohort", "prior_rung": "concept",
                                   "cohort_label": "pizzerias on Cavnar AI"}
    rows = _conf("C.rows(C.norm(c))", c=c)
    acc = [r for r in rows if r["key"] == "accuracy"][0]
    assert acc["detail"] == "Compared with pizzerias on Cavnar AI"
