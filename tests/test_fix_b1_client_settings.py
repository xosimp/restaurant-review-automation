"""Fix round B1 — the legacy client-settings page and its save (#8, #113,
#110, #73, #142).

What these protect:
- A save writes only the fields the admin touched. Every save used to post
  ~60 fields, so fixing one typo reverted the owner's newer edits, reset a
  past_due or internal billing state to 'trial', blanked an RPOWER POS label,
  and dropped the payroll week while the page said "Saved".
- Optimistic concurrency: a field somebody else changed since the page
  loaded is refused with a 409, never reverted — and a row that moved on only
  in fields the save doesn't touch (a POS sync, a token refresh) saves.
- Unknown keys are refused; every stored billing state, timezone and POS
  label is rendered; a billing change is an explicit, reasoned, recorded
  override.
- Review fetching follows create-client's rule on every save and has an
  explicit switch (and a console route).
- Numbers are bounded, JSON settings parsed, a timezone typo refused.
"""
import json
import os
import re
import shutil
import subprocess

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import models
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, restaurant_version, update_restaurant

CSRF = "b1-settings-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    import ops
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.secret_key = "b1-settings"
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    flask_app.register_blueprint(client_api.client_bp)

    @flask_app.template_filter("format_date")
    def _fd(v):
        return str(v)
    return flask_app


def _admin_client(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c, home


def _client_row(db_path, **kw):
    fields = dict(name="Simple Grill", owner_email="owner@grill.test")
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _save(c, rid, body):
    return c.post(f"/admin/client-settings/{rid}", json=body, headers={"X-CSRF": CSRF})


def _loaded(rid):
    """What the page embeds when it renders: the version and stored values."""
    return restaurant_version(rid), admin_routes.settings_loaded_values(get_restaurant(rid))


def _body(rid, version, loaded, **fields):
    return dict(fields, expected_version=version,
                base={("monthly_revenue_target" if k == "weekly_revenue_target" else k):
                      loaded.get("monthly_revenue_target" if k == "weekly_revenue_target" else k) for k in fields})


# ── #8: every stored state is rendered; nothing untouched is sent ───────────

def test_the_page_renders_every_stored_billing_state_pos_label_and_timezone(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="past_due", pos_system="RPOWER", timezone="America/Detroit")
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    assert re.search(r'<option value="past_due" selected>Past due</option>', html)
    assert re.search(r'<option value="RPOWER" selected>RPOWER</option>', html)
    assert re.search(r'<option value="America/Detroit" selected>America/Detroit \(as stored\)</option>', html)
    assert '<select id="billing_status" disabled>' in html, "billing is Stripe's: an override unlocks it"
    # Prices from pricing.py, not the launch prices.
    assert "$300/mo" not in html and "$500 setup" not in html
    assert "$750 + $349/mo" in html and "$3,000 + $1,199/mo" in html
    # The page carries what it loaded, for the server's conflict check.
    assert "var CAV_VERSION = " in html and "var CAV_LOADED = " in html


def test_an_unknown_stored_billing_state_is_its_own_selected_option(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="canceled")
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    assert re.search(r'<option value="canceled" selected>canceled \(as stored\)</option>', html)


def test_a_one_field_save_leaves_billing_pos_and_payroll_week_alone(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="past_due", pos_system="RPOWER")
    update_restaurant(rid, {"week_start_day": 6, "labor_target_pct": 27.0})
    v, loaded = _loaded(rid)
    r = _save(c, rid, _body(rid, v, loaded, voice_notes="Warm, never corporate"))
    assert r.status_code == 200 and r.get_json()["ok"] is True, r.get_json()
    row = get_restaurant(rid)
    assert row.voice_notes == "Warm, never corporate"
    assert (row.billing_status, row.pos_system, row.week_start_day, row.labor_target_pct) == \
        ("past_due", "RPOWER", 6, 27.0)
    assert r.get_json()["version"] == restaurant_version(rid)


# ── #113: the payroll week saves; unknown keys are refused ───────────────────

def test_week_start_day_is_saved_and_bounded(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    v, loaded = _loaded(rid)
    assert _save(c, rid, _body(rid, v, loaded, week_start_day="6")).get_json()["ok"] is True
    assert get_restaurant(rid).week_start_day == 6
    bad = _save(c, rid, {"week_start_day": 9})
    assert bad.status_code == 400 and "Payroll week start" in bad.get_json()["error"]
    assert get_restaurant(rid).week_start_day == 6


def test_an_unknown_key_is_refused_and_nothing_is_written(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    r = _save(c, rid, {"name": "Renamed", "service_tier": "full", "is_demo": 1})
    assert r.status_code == 400
    assert r.get_json()["unknown_fields"] == ["is_demo", "service_tier"]
    assert get_restaurant(rid).name == "Simple Grill"


# ── #113: optimistic concurrency ─────────────────────────────────────────────

def test_a_field_the_owner_changed_since_the_page_loaded_is_a_409_not_a_revert(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    update_restaurant(rid, {"labor_target_pct": 30.0, "sched_notes": "Two closers Fridays"})
    v, loaded = _loaded(rid)
    # The owner raises the target and edits the notes after the admin loaded.
    update_restaurant(rid, {"labor_target_pct": 28.0, "sched_notes": "Two closers Fri and Sat"})
    r = _save(c, rid, _body(rid, v, loaded, labor_target_pct=32, sched_notes="One closer"))
    assert r.status_code == 409
    body = r.get_json()
    assert body["conflict"] is True and body["fields"] == ["labor_target_pct", "sched_notes"]
    assert body["current"]["labor_target_pct"] == 28.0
    assert "Labor target" in body["error"] and "Scheduling notes" in body["error"]
    row = get_restaurant(rid)
    assert (row.labor_target_pct, row.sched_notes) == (28.0, "Two closers Fri and Sat")


def test_a_row_that_moved_on_only_elsewhere_saves_and_keeps_the_other_change(app, db_path):
    """A POS sync stamp or a token refresh bumps the row version all day;
    that must not make every admin save a conflict."""
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    v, loaded = _loaded(rid)
    update_restaurant(rid, {"toast_last_synced": "2026-09-29T10:00:00", "labor_target_pct": 26.0})
    r = _save(c, rid, _body(rid, v, loaded, voice_notes="Friendly"))
    assert r.status_code == 200, r.get_json()
    row = get_restaurant(rid)
    assert row.voice_notes == "Friendly"
    assert row.toast_last_synced == "2026-09-29T10:00:00" and row.labor_target_pct == 26.0


def test_a_field_sent_without_its_loaded_value_is_refused_once_the_row_moved(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    v, _loaded_values = _loaded(rid)
    update_restaurant(rid, {"toast_last_synced": "2026-09-29T10:00:00"})
    r = _save(c, rid, {"voice_notes": "Friendly", "expected_version": v})
    assert r.status_code == 409 and r.get_json()["fields"] == ["voice_notes"]


def test_both_sides_agreeing_is_not_a_conflict(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    v, loaded = _loaded(rid)
    update_restaurant(rid, {"never_say": "delightful"})
    r = _save(c, rid, _body(rid, v, loaded, never_say="delightful"))
    assert r.status_code == 200 and r.get_json()["changed"] == []


# ── #8: billing is an explicit, reasoned, recorded override ─────────────────

def test_a_billing_change_needs_a_reason_and_is_recorded_with_it(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="past_due")
    no_reason = _save(c, rid, {"billing_status": "active"})
    assert no_reason.status_code == 400 and "reason" in no_reason.get_json()["error"]
    assert get_restaurant(rid).billing_status == "past_due"
    ok = _save(c, rid, {"billing_status": "active", "billing_status_reason": "Paid by check on 9/28"})
    assert ok.get_json()["ok"] is True and get_restaurant(rid).billing_status == "active"
    conn = models.get_conn(db_path)
    ev = conn.execute("SELECT summary, payload FROM admin_events WHERE event_type='billing_status.override'").fetchone()
    act = conn.execute("SELECT event_data FROM activity_log WHERE event_type='admin_settings_update' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert "past_due" in ev["summary"] and "Paid by check" in ev["summary"]
    assert json.loads(ev["payload"])["actor"] == "will"
    data = json.loads(act["event_data"])
    assert data["changed"]["billing_status"] == {"from": "past_due", "to": "active"}
    assert data["billing_override_reason"] == "Paid by check on 9/28"


def test_resending_the_stored_billing_state_needs_no_reason(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="active")
    r = _save(c, rid, {"billing_status": "active", "name": "Simple Grill 2"})
    assert r.get_json()["ok"] is True and get_restaurant(rid).name == "Simple Grill 2"


def test_an_unlisted_billing_state_cannot_be_set(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    r = _save(c, rid, {"billing_status": "gold", "billing_status_reason": "x"})
    assert r.status_code == 400 and get_restaurant(rid).billing_status == "trial"


def test_leaving_paused_clears_the_resume_date(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    update_restaurant(rid, {"billing_status": "paused", "paused_until": "2026-10-15"})
    r = _save(c, rid, {"billing_status": "active", "billing_status_reason": "Owner asked to resume early"})
    assert r.get_json()["ok"] is True
    row = get_restaurant(rid)
    assert row.billing_status == "active" and row.paused_until is None


# ── #110: review fetching ─────────────────────────────────────────────────

def test_saving_a_place_id_with_reviews_on_turns_fetching_on(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, module_reviews=1)
    assert get_restaurant(rid).reviews_live == 0
    r = _save(c, rid, {"google_place_id": "ChIJnewplace"})
    assert r.get_json()["ok"] is True
    assert get_restaurant(rid).reviews_live == 1


def test_the_explicit_switch_wins_and_needs_something_to_fetch(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, module_reviews=1)
    refused = _save(c, rid, {"reviews_live": 1})
    assert refused.status_code == 400 and "Place ID" in refused.get_json()["error"]
    assert _save(c, rid, {"google_place_id": "ChIJx", "reviews_live": 0}).get_json()["ok"] is True
    assert get_restaurant(rid).reviews_live == 0, "switched off explicitly, the rule doesn't override it"
    assert _save(c, rid, {"reviews_live": 1}).get_json()["ok"] is True
    assert get_restaurant(rid).reviews_live == 1


def test_clearing_the_place_id_with_no_business_profile_stops_fetching(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, google_place_id="ChIJold", reviews_live=1)
    assert _save(c, rid, {"google_place_id": ""}).get_json()["ok"] is True
    row = get_restaurant(rid)
    assert row.google_place_id is None and row.reviews_live == 0


def test_a_demo_sharing_a_live_listing_never_fetches_it(app, db_path):
    c, _ = _admin_client(app, db_path)
    _client_row(db_path, name="Erik's Live", google_place_id="ChIJshared")
    demo = _client_row(db_path, name="Demo Copy", is_demo=1, module_reviews=1)
    assert _save(c, demo, {"google_place_id": "ChIJshared"}).get_json()["ok"] is True
    assert get_restaurant(demo).reviews_live == 0
    refused = _save(c, demo, {"reviews_live": 1})
    assert refused.status_code == 400 and "Erik's Live" in refused.get_json()["error"]


def test_the_console_switch_applies_the_same_rule(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, google_place_id="ChIJok")
    r = c.post(f"/admin/api/client/{rid}/reviews-fetching", json={"on": True}, headers={"X-CSRF": CSRF})
    assert r.get_json() == {"ok": True, "reviews_live": 1, "via": "places"}
    assert get_restaurant(rid).reviews_live == 1
    bare = _client_row(db_path, name="No Listing")
    r = c.post(f"/admin/api/client/{bare}/reviews-fetching", json={"on": True}, headers={"X-CSRF": CSRF})
    assert r.status_code == 400


# ── #142: bounds, JSON and timezone are checked ─────────────────────────────

@pytest.mark.parametrize("field,value,needle", [
    ("hourly_rate", 0, "Blended hourly rate"),
    ("hourly_rate", "abc", "Blended hourly rate"),
    ("food_cost_target", 95, "Food cost target"),
    ("waste_target_pct", -1, "Waste target"),
    ("alert_rating_floor", 6, "Rating alert floor"),
    ("section_count", 31, "Dining sections"),
    ("delivery_pct", 101, "Delivery %"),
    ("monthly_revenue_target", "1e12", "Monthly revenue target"),
    ("timezone", "America/Chicgo", "timezone"),
    ("owner_email", "not-an-email", "Owner email"),
    ("name", "   ", "Restaurant name"),
    ("digest_day", "someday", "Digest day"),
    ("pos_system", "Galaxy POS", "POS system"),
    ("menu_url", "javascript:alert(1)", "Menu URL"),
    ("role_rates_json", "{Server: 9", "Per-role hourly rates"),
    ("role_rates_json", '{"Server": 900}', "Server"),
    ("role_rates_json", '["Server", 9]', "JSON object"),
    ("role_minimums_json", '{"Cook": 1.5}', "whole number"),
    ("close_times_json", '{"Funday": "9pm"}', "Funday"),
    ("close_times_json", '{"Monday": "late"}', "late"),
    ("role_close_buffer_json", '{"Bartender": 600}', "Bartender"),
])
def test_a_bad_value_is_refused_with_a_sentence_and_nothing_changes(app, db_path, field, value, needle):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    before = get_restaurant(rid)
    r = _save(c, rid, {field: value, "voice_notes": "should not land"})
    assert r.status_code == 400, r.get_json()
    assert needle in r.get_json()["error"]
    after = get_restaurant(rid)
    assert getattr(after, field) == getattr(before, field) and after.voice_notes is None


def test_good_values_are_stored_normalised(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    r = _save(c, rid, {"role_rates_json": '{"Server": 9, " Cook ": "22.5"}', "close_times_json": '{"monday": "9:00pm"}',
                       "role_minimums_json": '{"Cook": 2}', "delivery_pct": "0", "timezone": "America/Los_Angeles",
                       "pos_system": "RPOWER", "section_count": "", "waste_target_pct": ""})
    assert r.get_json()["ok"] is True, r.get_json()
    row = get_restaurant(rid)
    assert json.loads(row.role_rates_json) == {"Server": 9.0, "Cook": 22.5}
    assert json.loads(row.close_times_json) == {"Monday": "9:00pm"}
    assert json.loads(row.role_minimums_json) == {"Cook": 2}
    assert row.delivery_pct == 0, "0% delivery is an answer, not blank"
    assert row.timezone == "America/Los_Angeles" and row.pos_system == "RPOWER"
    assert row.section_count is None and row.waste_target_pct is None
    assert _save(c, rid, {"role_rates_json": ""}).get_json()["ok"] is True
    assert get_restaurant(rid).role_rates_json is None


# ── the page's own JS: only touched fields, with what it loaded ─────────────

def _page_script(html):
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    return scripts[-1]


_HARNESS = r"""
var __els = {};
function __El(id) { this.id = id; this.innerHTML = ''; this.textContent = ''; this.value = '';
  this.checked = false; this.style = {}; this.disabled = false; this.type = 'text'; this.className = ''; this._ls = {}; }
__El.prototype.addEventListener = function (t, f) { (this._ls[t] = this._ls[t] || []).push(f); };
__El.prototype.fire = function (t) { var ls = this._ls[t] || []; for (var i = 0; i < ls.length; i++) ls[i]({target: this}); };
__El.prototype.focus = function () {};
var document = {getElementById: function (id) { return __els[id] || (__els[id] = new __El(id)); },
                querySelector: function () { return new __El('q'); }, hidden: false};
var window = {location: {pathname: '/admin/client-settings/7', reload: function () {}}};
var location = window.location;
var __fetches = [];
var __reply = function (url, opts) { return {status: 200, body: {ok: true, templates: [], ingredients: [],
  menu_items: [], priority_ingredients: []}}; };
function fetch(url, opts) {
  __fetches.push({url: url, opts: opts || {}});
  var r = __reply(url, opts || {});
  return Promise.resolve({status: r.status, json: function () { return Promise.resolve(r.body); }});
}
function confirm() { return true; } function alert() {} function prompt() { return 'Paid by check'; }
function FormData() { this.d = []; } FormData.prototype.append = function (k, v) { this.d.push([k, v]); };
"""


def _run_page(script, actions):
    js = _HARNESS + script + "\n" + actions
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_page_sends_only_the_touched_fields_with_their_loaded_values(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="past_due", voice_notes="old voice")
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    out = _run_page(_page_script(html), r"""
document.getElementById('voice_notes').value = 'new voice';
document.getElementById('voice_notes').fire('input');
saveSettings();
setTimeout(function () {
  var posts = __fetches.filter(function (f) { return f.opts.method === 'POST'; });
  console.log(JSON.stringify({posts: posts.map(function (p) { return {url: p.url, body: JSON.parse(p.opts.body)}; }),
                              status: document.getElementById('save-status').textContent}));
}, 50);""")
    assert len(out["posts"]) == 1
    body = out["posts"][0]["body"]
    assert set(body) == {"voice_notes", "base", "expected_version", "touched"}
    assert body["voice_notes"] == "new voice" and body["base"] == {"voice_notes": "old voice"}
    assert body["expected_version"] == restaurant_version(rid)
    assert out["status"] == "✓ Saved"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_save_with_nothing_touched_sends_nothing(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    out = _run_page(_page_script(html), r"""
saveSettings();
setTimeout(function () {
  console.log(JSON.stringify({posts: __fetches.filter(function (f) { return f.opts.method === 'POST'; }).length,
                              status: document.getElementById('save-status').textContent}));
}, 50);""")
    assert out["posts"] == 0 and "Nothing to save" in out["status"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_409_names_the_fields_escaped_and_offers_a_reload(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path)
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    out = _run_page(_page_script(html), r"""
__reply = function (url, opts) {
  if (opts.method === 'POST') return {status: 409, body: {ok: false, conflict: true, fields: ['sched_notes'],
    labels: {sched_notes: 'Scheduling notes'}, current: {sched_notes: '<img src=x onerror=alert(1)>'}}};
  return {status: 200, body: {ok: true, templates: [], ingredients: [], menu_items: [], priority_ingredients: []}};
};
document.getElementById('sched_notes').fire('input');
saveSettings();
setTimeout(function () { console.log(JSON.stringify({html: document.getElementById('save-status').innerHTML})); }, 50);""")
    assert "Scheduling notes" in out["html"] and "Reload" in out["html"]
    assert "<img" not in out["html"] and "&lt;img src=x onerror=alert(1)&gt;" in out["html"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_billing_override_sends_the_reason(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = _client_row(db_path, billing_status="past_due")
    html = c.get(f"/admin/client-settings/{rid}").get_data(as_text=True)
    out = _run_page(_page_script(html), r"""
cavBillingOverride(null);
var sel = document.getElementById('billing_status');
sel.value = 'active'; sel.fire('change');
saveSettings();
setTimeout(function () {
  var p = __fetches.filter(function (f) { return f.opts.method === 'POST'; })[0];
  console.log(JSON.stringify(JSON.parse(p.opts.body)));
}, 50);""")
    assert out["billing_status"] == "active" and out["billing_status_reason"] == "Paid by check"
    assert out["base"]["billing_status"] == "past_due"
