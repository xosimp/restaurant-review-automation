"""Web client audit fixes (templates/dashboard.html).

Error text never reaches a public post; model and guest text is escaped in
the content calendar and the guest list; the hidden attribute always hides;
the owner-only Account controls are drawn only for the account holder; the
export picker offers only what the export route allows; the review editor's
Cancel drops the edit; deleting the open Ask chat starts a new one; a save
over a gone draft lets go of its id.

Source rules read the template (the rule must hold on every path, not only
the one a fixture renders); the Account gating is also rendered for an owner
and a manager through the real app.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    """The body of a top-level `function name(` in the template."""
    start = SRC.index("function " + name + "(")
    return SRC[start:SRC.index("\n}\n", start)]


# ── 1. A failed generation is never posted ────────────────────────────────

def test_every_post_path_reads_the_composer_through_mktbody():
    for fn in ("postToInstagram", "postToFacebook", "postToGoogle", "mktSendAsNewsletter"):
        body = _fn(fn)
        assert "_mktBody()" in body, fn
        assert "innerText" not in body, fn
    assert "data-gen-error" in _fn("_mktBody") and "data-mkt-posted" in _fn("_mktBody")


def test_a_failed_generation_hides_the_post_buttons_an_earlier_one_showed():
    gen = _fn("genContent")
    # Both failure paths: the server's refusal and the network catch.
    assert gen.count("setAttribute('data-gen-error','1')") == 2
    assert gen.count("_mktHidePostBtns()") == 2
    hide = _fn("_mktHidePostBtns")
    for bid in ("ig-post-btn", "fb-post-btn", "gg-post-btn"):
        assert bid in hide


# ── 2. Model and guest text is text ───────────────────────────────────────

def test_the_content_calendar_escapes_every_model_field():
    cal = _fn("renderCal")
    for field in ("i.day", "i.platform", "i.angle", "i.date"):
        assert "_e(" + field + ")" in cal, field
    assert "'+i.day+" not in cal and "( i.angle||'')" not in cal
    assert "_escHtml(String(d.error" in _fn("loadCal")


def test_a_guest_contacts_name_and_phone_are_escaped():
    i = SRC.index("text-overflow:ellipsis\">' + _escHtml(String(c.name || c.phone || ''))")
    assert "_escHtml(String(c.phone || ''))" in SRC[i:i + 400]


# ── 5. [hidden] always hides ──────────────────────────────────────────────

def test_the_hidden_attribute_beats_class_display_everywhere():
    start = SRC.index("\n<style>\n")
    first_style = SRC[start:SRC.index("</style>", start)]
    assert "[hidden]{display:none!important}" in first_style


# ── 3. Owner-only Account controls ────────────────────────────────────────

def test_billing_js_handles_owner_only_instead_of_no_subscription():
    fn = _fn("loadBillingInfo")
    assert "d.owner_only" in fn
    assert fn.index("d.owner_only") < fn.index("billing-no-sub")


# ── 4. Export picker ──────────────────────────────────────────────────────

def test_export_picker_is_built_from_the_servers_scopes_and_names_refusals():
    i = SRC.index('class="exp-scope" id="exp-first"')
    picker = SRC[i:i + 900]
    assert "'labor' in _exp" in picker and "'food_cost' in _exp" in picker
    assert "{% if mod_labor %}<label><input type=\"checkbox\" class=\"exp-scope\"" not in picker
    j = SRC.index("window.exportData=function")
    assert "d.refused" in SRC[j:j + 1200]


# ── 6. Review editor Cancel ───────────────────────────────────────────────

def test_cancel_clears_the_pending_autosave_and_restores_the_stored_draft():
    fn = _fn("closeEditor")
    assert "clearTimeout(_revAutoT[id])" in fn and "delete _revAutoT[id]" in fn
    assert "ta.value = tx.textContent" in fn


# ── 7. Deleting the open Ask chat ─────────────────────────────────────────

def test_deleting_the_open_chat_resets_like_new_chat():
    i = SRC.index("window.askDeleteConversation=function")
    body = SRC[i:SRC.index("};", i)]
    assert "askResetToNew()" in body
    j = SRC.index("function askResetToNew()")
    reset = SRC[j:SRC.index("\n  }\n", j)]
    for bit in ("window._askNewChat=true", "_askCavnarHistory=[]", "sessionStorage.removeItem(ASK_CONV_KEY)",
                "_askEmptyHtml()"):
        assert bit in reset, bit


# ── 8. A save over a gone draft ───────────────────────────────────────────

def test_a_gone_or_expired_draft_releases_the_composers_id():
    fn = _fn("saveMktDraft")
    assert "d.code === 'draft_gone'" in fn and "d.code === 'draft_expired'" in fn
    assert fn.count("window._mktDraftId = null") == 2


# ── 3 and 4, rendered: an owner and a manager through the real app ────────

RENDER = r'''
import json, os, re, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Gate Co", owner_email="own@x.test", module_reviews=1,
                                          module_labor=1, module_inventory=1, module_marketing=1))
auth.create_user(rid, "own", "own@x.test", "correct-horse-battery", role="client")
auth.create_user(rid, "gm", "gm@x.test", "correct-horse-battery", role="manager")
out = {}
for who in ("own", "gm"):
    c = h.app.test_client()
    page = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = c.post("/login", data={"username": who, "password": "correct-horse-battery", "csrf_token": token})
    assert r.status_code in (302, 303), (who, r.status_code)
    r = c.get("/")
    assert r.status_code == 200, r.get_data(as_text=True)[-800:]
    html = r.get_data(as_text=True)
    notify = re.search(r'<input[^>]*id="as-login-notify"[^>]*>', html).group(0)
    out[who] = {
        "principal": 'data-principal="1"' in html,
        "billing_card": 'id="billing-card"' in html,
        "billing_note": "Only the account owner can see billing" in html,
        "sub_item": 'class="ac-hitem" data-health="subscription"' in html,
        "qa_billing": 'id="ac-qa-billing"' in html,
        "close_card": 'id="acct-close-card"' in html,
        "alerts_btn": 'onclick="openAlertsModal()"' in html,
        "alerts_note": "Only the account owner can change alerts." in html,
        "notify_disabled": " disabled" in notify,
        "google_connect": 'onclick="gmbConnect()"' in html,
        "ig_connect": 'onclick="igConnect()"' in html,
        "pos_connect": 'onclick="openToastClientModal()"' in html,
        "exp_labor": 'class="exp-scope" value="labor"' in html,
        "exp_food": 'class="exp-scope" value="food_cost"' in html,
    }
print(json.dumps(out))
'''


def test_owner_only_account_controls_render_for_the_owner_alone():
    vol = tempfile.mkdtemp(prefix="cavnar-gate-")
    run = subprocess.run([sys.executable, "-c", RENDER, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stdout[-1500:] + "\n" + run.stderr[-2500:]
    got = json.loads(run.stdout.strip().splitlines()[-1])
    own, gm = got["own"], got["gm"]
    # The account holder sees every control.
    assert own["principal"] and own["billing_card"] and own["sub_item"] and own["qa_billing"], own
    assert own["close_card"] and own["alerts_btn"] and not own["notify_disabled"], own
    assert own["google_connect"] and own["ig_connect"] and own["pos_connect"], own
    assert own["exp_labor"] and own["exp_food"], own
    # A manager: a note where billing was, none of the controls that 403.
    assert not gm["principal"] and not gm["billing_card"] and gm["billing_note"], gm
    assert not gm["sub_item"] and not gm["qa_billing"] and not gm["close_card"], gm
    assert not gm["alerts_btn"] and gm["alerts_note"] and gm["notify_disabled"], gm
    assert not gm["google_connect"] and not gm["ig_connect"] and not gm["pos_connect"], gm
    # Labor is in a manager's view; food cost margins are not (FOOD_COST_VIEW).
    assert gm["exp_labor"] and not gm["exp_food"], gm
