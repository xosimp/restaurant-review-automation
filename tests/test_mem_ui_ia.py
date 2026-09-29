"""Memory round, UI wave — iOS part A (UI-IA): the phone shows what the
memory workstreams put into the payloads (Home's cards and answers, the
brief, the nightly report, Ask, Account → Memory / Trust / Notifications /
Change history, Data Health, the K1 Why panel, the policy notice).

Two kinds of test, the model the admin round's tests/test_fix_ui_ui*.py set:

- behaviour on the server where the phone depends on a payload the round
  had not carried to it (the Home attention remap);
- source pins: each new iOS element is tied to the exact field it reads —
  the server must produce the key (read from the producing function) and
  the Swift decoder must read that key. A renamed field fails here, not on
  Will's phone. Swift behaviour itself is pinned by
  ios/CavnarAI/CavnarAITests/MemoryRoundIATests.swift (xcodebuild test).

No model, network, email, SMS or push is reached.
"""
import os
import re
import sys

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _swift(rel):
    with open(os.path.join(IOS, rel), encoding="utf-8") as f:
        return f.read()


def _all_swift():
    out = {}
    for base, _dirs, files in os.walk(IOS):
        for name in files:
            if name.endswith(".swift"):
                p = os.path.join(base, name)
                with open(p, encoding="utf-8") as f:
                    out[os.path.relpath(p, IOS)] = f.read()
    return out


def _py(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── the attention remap carries the memory fields (behaviour) ───────────────

@pytest.fixture
def db(db_path, monkeypatch):
    import auth
    import home_brief
    import models
    import pos
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    auth.init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes
    init_two_fa_backup_codes(db_path=db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    from auth_routes import _login_attempts
    _login_attempts.clear()
    home_brief.invalidate()
    yield db_path
    home_brief.invalidate()


@pytest.fixture
def client(db):
    import client_api
    import mobile_api
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def test_the_phones_attention_items_carry_what_was_said_before(client, db, monkeypatch):
    """The remap to the phone's flat attention list dropped previous_answer,
    delegate_answer and conflict: web Home read them off the item and the
    phone never got them."""
    from datetime import datetime, timedelta
    import rec_ledger
    from auth import create_user
    from models import Restaurant, create_restaurant, get_conn
    rid = create_restaurant(Restaurant(name="Memory Co", owner_email="m@x.com", timezone="America/Chicago",
                                       module_reviews=1), db_path=db)
    create_user(rid, "owner1", "owner1@x.com", "memory-pass-1", db_path=db)
    headers = {"Authorization": "Bearer " + client.post(
        "/mobile/api/login", json={"username": "owner1", "password": "memory-pass-1"}).get_json()["token"]}
    c = get_conn(db)
    for i in range(3):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                  "response_status, draft_response, urgency, draft_needs_review, review_date, fetched_at) "
                  "VALUES (?, 'yelp', ?, 'Ann', 4, 'Nice night', 1, 'drafted', 'Thanks!', 'normal', 0, ?, ?)",
                  (rid, f"m{i}", (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()
    said = {"answer": "dismissed", "answered_on": "9/1/26", "text": "You said not for us to this on 9/1/26."}

    def prev(_rid, keys, db_path=None):
        return {k: said for k in keys}
    monkeypatch.setattr(rec_ledger, "previous_answers", prev)
    phone = client.get("/mobile/api/home", headers=headers).get_json()
    items = phone["needs_attention"]
    assert items, "the fixture should put the drafted replies on Home"
    for it in items:
        for key in ("previous_answer", "delegate_answer", "conflict"):
            assert key in it, f"{key} is dropped by the phone's attention remap"
    assert any((it.get("previous_answer") or {}).get("text") == said["text"] for it in items)


# ── source pins: server key → Swift decoder ─────────────────────────────────

# (what the phone draws, the Swift file, the server key it reads)
PINS = [
    ("history line (own answer)", "Models/HomeSummary.swift", "previous_answer"),
    ("history line (delegate)", "Models/HomeSummary.swift", "delegate_answer"),
    ("re-test", "Models/HomeSummary.swift", "retest"),
    ("conflict on a card", "Models/HomeSummary.swift", "conflict"),
    ("trim caution", "Models/HomeSummary.swift", "caution"),
    ("kind holds", "Models/HomeSummary.swift", "kind_holds"),
    ("quieter re-test date", "Models/HomeSummary.swift", "review_on"),
    ("policy notice", "Models/HomeSummary.swift", "policy_notice"),
    ("kind hold answers", "Models/RecMemory.swift", "not_for_us"),
    ("kind hold record", "Models/RecMemory.swift", "do_nothing_pct"),
    ("conflict choices", "Models/RecMemory.swift", "signature"),
    ("the report's calls", "Features/Home/HomeDay.swift", "predictions"),
    ("the report's forecast record", "Features/Home/HomeDay.swift", "confidence_pct"),
    ("DSR action caution/conflict", "Features/DailyReport/DailyReportModels.swift", "conflict"),
    ("DSR budget goal", "Features/DailyReport/DailyReportFormat.swift", '"goal"'),
    ("DSR forecast reason", "Features/DailyReport/DailyReportFormat.swift", '"reason"'),
    ("DSR weather (actual)", "Features/DailyReport/DailyReportFormat.swift", '"observed"'),
    ("Ask declined repeats", "Features/AskCavnar/AskCavnarViewModel.swift", "declined_repeats"),
    ("Ask declined repeats (stream)", "Core/APIClient.swift", "declined_repeats"),
    ("Ask rating preference", "Features/AskCavnar/AskCavnarViewModel.swift", "preference"),
    ("Ask proposed goal", "Features/AskCavnar/AskCavnarViewModel.swift", "proposed"),
    ("Memory: forget", "Features/Account/AccountMemoryView.swift", "can_forget"),
    ("Memory: restore", "Features/Account/AccountMemoryView.swift", "can_restore"),
    ("Memory: archive reason", "Features/Account/AccountMemoryView.swift", "reason_label"),
    ("Memory: until", "Features/Account/AccountMemoryView.swift", "valid_until_label"),
    ("Memory: due", "Features/Account/AccountMemoryView.swift", "due_label"),
    ("Memory: add", "Features/Account/AccountMemoryView.swift", "valid_until"),
    ("proposed goals", "Features/Home/HomeFollowThrough.swift", "proposed_by"),
    ("proposed goals confirm", "Features/Home/HomeFollowThrough.swift", "can_confirm"),
    ("trust lapses", "Features/Account/AccountAutomationView.swift", "lapsed_on"),
    ("trust earned date", "Features/Account/AccountAutomationView.swift", "earned_at"),
    ("trust weak credit", "Features/Account/AccountAutomationView.swift", "weak_credit"),
    ("schedule undo", "Features/Account/AccountAutomationView.swift", "undone_on"),
    ("supplier undo", "Features/Account/AccountAutomationView.swift", "clean_since_undo"),
    ("change history", "Features/Account/AccountActivityLogView.swift", "line"),
    ("change history date", "Features/Account/AccountActivityLogView.swift", "changed_at"),
    ("my notifications", "Features/Account/AccountMyNotifications.swift", "push_muted_types"),
    ("unmutable types", "Features/Account/AccountMyNotifications.swift", "unmutable_types"),
    ("apply to all", "Features/Account/AccountMyNotifications.swift", "can_apply_to_all"),
    ("never opened (mine)", "Features/Account/AccountMyNotifications.swift", "alert_type"),
    ("data you don't trust", "Models/DataHealth.swift", "distrusted"),
    ("K1 profile unlock", "Models/TrustConfidence.swift", "prior_unlock"),
    ("K1 prior rung", "Models/TrustConfidence.swift", "prior_rung"),
    ("undo why", "DesignSystem/RecMemoryViews.swift", "ask_why"),
]


@pytest.mark.parametrize("what,rel,key", PINS, ids=[p[0] for p in PINS])
def test_each_new_element_reads_its_field(what, rel, key):
    src = _swift(rel)
    needle = key if key.startswith('"') else f'"{key}"'
    assert needle in src or re.search(r"\bcase " + re.escape(key) + r"\b", src) \
        or re.search(r"case [^\n]*\b" + re.escape(key) + r"\b", src), \
        f"{what}: {rel} no longer reads `{key}`"


def test_the_server_produces_the_keys_the_phone_reads():
    """The producing functions still write the keys pinned above."""
    rl = _py("rec_ledger.py")
    assert '"text": text + "."' in rl and '"answered_on": on' in rl            # previous_answers
    assert 'r["retest"] = True' in _py("home_brief.py")
    assert '"kind_holds": kind_holds' in _py("home_brief.py")
    assert '"review_on":' in _py("home_brief.py")
    lc = _py("lever_conflicts.py")
    for k in ('"id": cf["id"]', '"with":', '"why": cf["why"]', '"choose": cf["choose"]', '"mobile": "/mobile/api/recs/conflict"'):
        assert k in lc, k
    hold = _py("rec_learning.py")
    assert '"do_nothing_pct"' in hold and '"not_for_us": "Stop suggesting it"' in hold
    mb = _py("morning_brief.py")
    assert '"confidence_pct": (conf or {}).get("pct"), "predictions": preds' in mb
    assert '"key": "dsr_action"' in mb and 'key in ("yesterday", "dsr_action")' in mb
    assert '"conflict"' in _py("lever_conflicts.py")
    om = _py("owner_memory.py")
    for k in ('"can_forget"', '"valid_until_label"', '"due_label"', '"reason_label"', '"can_restore"', '"lanes"'):
        assert k in om, k
    sr = _py("strategy_routes.py")
    for k in ('"proposed": props', '"can_confirm"', '"unmutable_types"', '"never_opened"', '"can_apply_to_all"',
              'out["lapsed"]', 'out["schedule"]["undone_on"]', '"ask_why"', '"/data-health/verify"',
              '"/recs/conflict"', '"/goals/<int:goal_id>/confirm"', '"/account/memory/restore"',
              '"/account/preferences/mine"', '"/account/preferences/apply-to-all"', '"preference": preference'):
        assert k in sr, k
    assert '"distrusted": distrusted_items' in _py("data_health.py")
    assert '"prior_unlock": r.get("prior_unlock")' in _py("confidence_engine.py")
    assert 'changes=changes' in _py("mobile_api.py")
    assert '"line": describe(row)' in _py("change_log.py")
    assert '"earned_at"' in _py("models.py") and '"weak_credit"' in _py("models.py")
    assert '"clean_since_undo"' in _py("ordering.py") or "clean_since_undo" in _py("ordering.py")
    ds = _py("dsr/block_sales.py")
    assert '"reason": (fc_any or {}).get("reason")' in ds and '"label": budget.get("label")' in ds
    assert "observed={" in _py("dsr/access.py")
    assert 'caution=guard["caution"]' in _py("dsr/narrative.py")


def test_the_activity_payload_is_read_with_its_changes():
    """/account/activity's `changes` rides on the synthesized decoder by its
    property name."""
    src = _swift("Features/Account/AccountViewModel.swift")
    assert "var changes: HomeLenientList<AccountChange>? = nil" in src
    assert "changes = response.changes?.items ?? []" in src


# ── the decline reads "Not for us" everywhere on iOS ─────────────────────────

def test_no_ios_control_says_pass():
    for rel, src in _all_swift().items():
        for m in re.finditer(r'"(Pass|Passed)"', src):
            line = src[:m.start()].count("\n") + 1
            ctx = src.splitlines()[line - 1]
            assert ctx.strip().startswith("//"), f'{rel}:{line} still says {m.group(0)}'
    assert 'case .notForUs:  return "Not for us"' in _swift("DesignSystem/RecAnswerRow.swift")


# ── the house rules the new screens must keep ───────────────────────────────

NEW_FILES = ["Models/RecMemory.swift", "DesignSystem/RecMemoryViews.swift", "Features/Home/HomeKindHolds.swift",
             "Features/Home/HomePolicyNotice.swift", "Features/Account/AccountMemoryView.swift",
             "Features/Account/AccountMyNotifications.swift"]


@pytest.mark.parametrize("rel", NEW_FILES)
def test_new_ios_screens_keep_the_house_rules(rel):
    src = _swift(rel)
    # Loading is the sliding pulse, never a spinner.
    assert "ProgressView(" not in src, f"{rel}: a spinner — use CavnarSkeletonBar"
    # No locale date formatting an owner would read (M/D/YY only).
    assert 'dateFormat = "MMM' not in src and "h:mm a" not in src, rel
    assert ".formatted(date:" not in src, rel
    # The product is Cavnar AI, never bare "Cavnar" in words an owner reads.
    for m in re.finditer(r'"[^"\n]*\bCavnar\b(?! AI)[^"\n]*"', src):
        assert "Cavnar AI" in m.group(0) or "cavnar.ai" in m.group(0).lower(), f"{rel}: {m.group(0)}"
    # No colour outside the named tokens.
    assert not re.search(r"Color\(red:|Color\(#|UIColor\(red:", src), f"{rel}: a raw colour"


def test_the_memory_sheet_never_uses_a_system_date_field():
    """Owner-facing dates are M/D/YY; a DatePicker's label is the locale's."""
    assert "DatePicker" not in _swift("Features/Account/AccountMemoryView.swift")


def test_confirm_cards_post_lists_intact():
    """AnyCodableValue keeps lists and objects: set_staff_unavailable's
    unavailable_days went out as null, and the availability route then saved
    the person as available every day."""
    src = _swift("Features/AskCavnar/AskCavnarViewModel.swift")
    assert "case array([AnyCodableValue])" in src and "case object([String: AnyCodableValue])" in src
    assert "indirect enum AnyCodableValue" in src


def test_the_policy_notice_opens_only_the_policy_page():
    src = _swift("Models/HomeBriefExtras.swift")
    assert 'u.host?.hasSuffix("cavnar.ai")' in src and '"https://cavnar.ai/privacy"' in src
