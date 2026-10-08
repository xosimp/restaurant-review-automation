"""Blind re-audit of the Reviews / Intel round (10/8/26).

#1  a skipped reply is approved only by an explicit "Approve after all"
    (`approve_skipped`); a queued replay, the lock screen, a bell row, Ask,
    the rule and a bulk publish all leave a skip standing.
#2  (push #3) the review push carries `draft_hash`, which survives every cut.
#3  the web's Approve and Save & approve send the words on the card.
#4  Ask's approve proposals bind each reply to its words.
#6  a post in flight is not a failed post (no Retry over it, no second post).
#7  Read now is offered only to logins the sync route accepts.
#8  Ask never hands over an answered-elsewhere review's unused draft.
#11 the website card under Intel reads an Intel-gated route.
"""
import json
import os
import sqlite3
import sys

import pytest
from flask import Flask

import ask_cavnar_tools
import auth
import client_api
import mobile_api
import models
import push
from models import Restaurant, Review, create_restaurant, save_reviews

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.test"), db_path=db_path)


def _review(rid, db_path, name, status="drafted", draft="Thanks, Ann!", **cols):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=name, author="Ann", rating=5,
                         text="Lovely", review_date=models.datetime.now().strftime("%Y-%m-%dT12:00:00"),
                         review_name=name)], db_path=db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE reviews SET response_status=?, draft_response=?, processed=1 WHERE review_name=?",
              (status, draft, name))
    for k, v in cols.items():
        c.execute(f"UPDATE reviews SET {k}=? WHERE review_name=?", (v, name))
    c.commit()
    out = c.execute("SELECT id FROM reviews WHERE review_name=?", (name,)).fetchone()[0]
    c.close()
    return out


def _status(db_path, review_id):
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()[0]
    finally:
        c.close()


def _user(rid):
    return {"id": 7, "restaurant_id": rid, "role": "owner", "email": "e@x.test"}


def _approve(rid, review_id, **kw):
    """A person's approve, inside a request (so it is not the rule's)."""
    app = Flask(__name__)
    with app.test_request_context("/approve/%d" % review_id, method="POST"):
        return client_api._do_approve(review_id, rid, user=_user(rid), **kw)


# ── #1 a skip stands unless the person says "Approve after all" ─────────────

def test_a_plain_approve_never_overturns_a_skip(rid, db_path):
    a = _review(rid, db_path, "g/skip-plain", status="skipped")
    payload, status = _approve(rid, a)
    assert status == 409 and payload["skipped"] is True and "Approve after all" in payload["error"]
    assert _status(db_path, a) == "skipped"


def test_a_queued_replay_made_before_the_skip_is_refused(rid, db_path):
    # The phone's offline queue replays {confirm_flagged, expected_draft}
    # — the words match, the skip still stands.
    a = _review(rid, db_path, "g/skip-replay", status="skipped")
    payload, status = _approve(rid, a, expected_draft="Thanks, Ann!")
    assert status == 409 and payload.get("skipped") is True
    assert _status(db_path, a) == "skipped"


def test_a_lock_screen_or_bell_approve_with_the_fingerprint_is_refused(rid, db_path):
    a = _review(rid, db_path, "g/skip-bell", status="skipped")
    payload, status = _approve(rid, a, expected_draft_hash=models.draft_hash("Thanks, Ann!"))
    assert status == 409 and payload.get("skipped") is True
    assert _status(db_path, a) == "skipped"


def test_approve_after_all_approves_it(rid, db_path):
    a = _review(rid, db_path, "g/skip-after-all", status="skipped")
    payload, status = _approve(rid, a, approve_skipped=True, expected_draft="Thanks, Ann!")
    assert status == 200 and payload["ok"] is True
    assert _status(db_path, a) == "approved"


def test_the_rule_and_a_bulk_publish_never_take_a_skip_even_when_told_to(rid, db_path):
    a = _review(rid, db_path, "g/skip-rule", status="skipped")
    payload, status = client_api._do_approve(a, rid, auto=True, approve_skipped=True)
    assert status == 409 and _status(db_path, a) == "skipped"
    payload, status = _approve(rid, a, bulk=True, approve_skipped=True)
    assert status == 409 and _status(db_path, a) == "skipped"
    # The default is the safe set, with no approver at all.
    assert models.claim_approval(a, rid, approver=None) is False


def test_both_routes_read_approve_skipped_only_as_a_literal_true():
    for src, fn in (("client_api.py", "def approve(rid, current_user):"),
                    ("mobile_api.py", "def mobile_approve_review(review_id, current_user):")):
        body = _read(src)
        body = body[body.index(fn):]
        body = body[:body.index("return jsonify")]
        assert 'approve_skipped=_body.get("approve_skipped") is True' in body


def test_ask_never_passes_approve_skipped():
    assert "approve_skipped" in ask_cavnar_tools.PROPOSAL_DENYLIST
    p = ask_cavnar_tools.proposal_args("approve_review", {"review_id": 3, "approve_skipped": True})
    assert "approve_skipped" not in p


def test_the_web_sends_approve_skipped_only_from_approve_after_all():
    dash = _read("templates/dashboard.html")
    card = _read("templates/_review_card.html")
    fn = dash[dash.index("function approveR(id, confirmed, skipped){"):]
    fn = fn[:fn.index("\n}).catch")]
    assert "if(skipped===true)_body.approve_skipped=true;" in fn
    # Every Approve after all passes it; no plain Approve does.
    assert dash.count("approveR('+id+',false,true)") == dash.count("Approve after all</button>") == 1
    assert card.count("approveR({{ r.id }},false,true)") == card.count("Approve after all</button>") == 1
    sd = dash[dash.index("function saveDraft(id) {"):]
    assert "approve_skipped" not in sd[:sd.index("\n}\n")]


def test_the_phone_sends_approve_skipped_only_from_approve_after_all():
    view = _read("ios/CavnarAI/CavnarAI/Features/Reviews/ReviewDetailView.swift")
    vm = _read("ios/CavnarAI/CavnarAI/Features/Reviews/ReviewDetailViewModel.swift")
    assert view.count("approveSkipped: true") == 1
    i = view.index("approveSkipped: true")
    assert "Approve after all" in view[i:i + 200]
    # Never queued: the offline replay carries no approve_skipped.
    q = vm[vm.index("private func queueApprove("):]
    q = q[:q.index("\n    }\n")]
    assert "approveSkipped" not in q


# ── #2 / push #3 the review push carries the reply's fingerprint ───────────

def test_the_push_draft_carries_its_hash_whole_or_clipped():
    whole = push._draft_fields("  Thanks, Ann!  ")
    assert whole["draft_complete"] is True and whole["draft_hash"] == models.draft_hash("Thanks, Ann!")
    long = "x" * (push.PUSH_DRAFT_MAX_CHARS + 50)
    clipped = push._draft_fields(long)
    assert clipped["draft_complete"] is False and clipped["draft_hash"] == models.draft_hash(long)


def test_the_hash_survives_when_the_draft_is_dropped_to_fit():
    big = "y" * 900
    payload = {"aps": {"alert": {"title": "t", "body": "z" * 3900}},
               "cavnar": dict(push._draft_fields(big), review_id=4)}
    out = push._fit_payload(payload)
    assert len(out) <= push.APNS_MAX_PAYLOAD_BYTES
    cav = json.loads(out)["cavnar"]
    assert "draft" not in cav and cav["draft_hash"] == models.draft_hash(big)


def test_a_clipped_push_approve_is_held_to_the_hash(rid, db_path):
    a = _review(rid, db_path, "g/push-clipped")
    payload, status = _approve(rid, a, expected_draft_hash=models.draft_hash("An older reply"))
    assert status == 409 and payload["draft_changed"] is True and _status(db_path, a) == "drafted"
    payload, status = _approve(rid, a, expected_draft_hash=models.draft_hash("Thanks, Ann!"))
    assert status == 200 and _status(db_path, a) == "approved"


def test_the_phone_sends_the_hash_from_the_lock_screen():
    pm = _read("ios/CavnarAI/CavnarAI/Push/PushManager.swift")
    assert 'case expectedDraftHash = "expected_draft_hash"' in pm
    assert "action.body = .approve(expectedDraft: shown, expectedDraftHash: hash)" in pm


# ── #3 the web approves the words on the card ───────────────────────────────

def test_the_web_approve_and_save_and_approve_send_expected_draft():
    dash = _read("templates/dashboard.html")
    fn = dash[dash.index("function approveR(id, confirmed, skipped){"):]
    fn = fn[:fn.index("\n}).catch")]
    assert "_body.expected_draft=_shownEl.textContent||'';" in fn and "JSON.stringify(_body)" in fn
    sd = dash[dash.index("function saveDraft(id) {"):]
    sd = sd[:sd.index("\n}\n")]
    assert "JSON.stringify({confirm_flagged:!!sd.needs_review, expected_draft:draft})" in sd


# ── #4 Ask's approve proposals bind the words ───────────────────────────────

def test_an_approve_review_card_binds_its_reply(rid, db_path):
    a = _review(rid, db_path, "g/ask-one")
    p = ask_cavnar_tools.build_proposal("approve_review", {"review_id": a}, restaurant_id=rid)
    assert p["body"]["expected_draft_hash"] == models.draft_hash("Thanks, Ann!")
    assert "expected_draft_hash" in {f["key"] for f in p["fields_shown"]}
    # The model can't set it.
    p2 = ask_cavnar_tools.build_proposal("approve_review", {"review_id": a, "expected_draft_hash": "abc"},
                                         restaurant_id=rid)
    assert p2["body"]["expected_draft_hash"] == models.draft_hash("Thanks, Ann!")
    # Rewritten after the card: Confirm posts nothing.
    c = sqlite3.connect(db_path)
    c.execute("UPDATE reviews SET draft_response='Something else' WHERE id=?", (a,))
    c.commit()
    c.close()
    payload, status = _approve(rid, a, expected_draft_hash=p["body"]["expected_draft_hash"])
    assert status == 409 and payload["draft_changed"] and _status(db_path, a) == "drafted"


def test_an_approve_all_card_binds_each_reply_and_a_rewritten_one_waits(rid, db_path, monkeypatch):
    import drafter
    monkeypatch.setattr(drafter, "check_reply", lambda text, *a, **k: (None, text))
    a = _review(rid, db_path, "g/ask-all-a", draft="Thanks, Ann!")
    b = _review(rid, db_path, "g/ask-all-b", draft="Thanks, Bo!")
    p = ask_cavnar_tools.build_proposal("approve_all_reviews", {}, restaurant_id=rid)
    body = p["body"]
    assert sorted(body["review_ids"]) == sorted([a, b])
    assert body["review_hashes"] == {str(a): models.draft_hash("Thanks, Ann!"),
                                     str(b): models.draft_hash("Thanks, Bo!")}
    assert {"review_ids", "review_hashes"} <= {f["key"] for f in p["fields_shown"]}
    c = sqlite3.connect(db_path)
    c.execute("UPDATE reviews SET draft_response='Thanks, Bo — see you soon!' WHERE id=?", (b,))
    c.commit()
    c.close()
    app = Flask(__name__)
    with app.test_request_context("/api/reviews/approve-all", method="POST"):
        out, status = client_api._do_approve_all(rid, review_ids=body["review_ids"], user=_user(rid),
                                                 review_hashes=body["review_hashes"])
    assert status == 200 and out["approved"] == 1 and out["changed"] == 1 and out["failed"] == 0
    assert _status(db_path, a) == "approved" and _status(db_path, b) == "drafted"


def test_a_listed_reply_with_no_fingerprint_does_not_post(rid, db_path, monkeypatch):
    import drafter
    monkeypatch.setattr(drafter, "check_reply", lambda text, *a, **k: (None, text))
    a = _review(rid, db_path, "g/nohash")
    app = Flask(__name__)
    with app.test_request_context("/api/reviews/approve-all", method="POST"):
        out, status = client_api._do_approve_all(rid, review_ids=[a], user=_user(rid), review_hashes={})
    assert out["approved"] == 0 and out["changed"] == 1 and _status(db_path, a) == "drafted"


def test_both_bulk_routes_pass_review_hashes():
    for src in ("client_api.py", "mobile_api.py"):
        assert 'review_hashes=data.get("review_hashes")' in _read(src)


# ── #6 a post in flight is not a failed post ───────────────────────────────

def test_a_post_in_flight_offers_no_retry_and_starts_no_second_post(rid, db_path):
    a = _review(rid, db_path, "g/in-flight", status="approved")
    assert client_api._claim_post_attempt(a, rid) is True
    row = {r["id"]: r for r in models.get_reviews_data(rid)}[a]
    assert models.review_post_failed(row, True) is False, "a post still waiting on Google"
    assert client_api._claim_post_attempt(a, rid) is False, "a Retry beside it never posts twice"
    client_api._record_post_error(a, rid, "Google said no")
    row = {r["id"]: r for r in models.get_reviews_data(rid)}[a]
    assert models.review_post_failed(row, True) is True and row["post_error"] == "Google said no"
    assert client_api._claim_post_attempt(a, rid) is True, "a failed post may be retried"


def test_a_stale_attempt_counts_as_finished(rid, db_path):
    a = _review(rid, db_path, "g/stale", status="approved",
                post_attempted_at="2020-01-01 00:00:00")
    row = {r["id"]: r for r in models.get_reviews_data(rid)}[a]
    assert models.review_post_failed(row, True) is True
    assert client_api._claim_post_attempt(a, rid) is True


def test_a_failed_post_is_recorded_on_the_row(rid, db_path, monkeypatch):
    import gmb
    a = _review(rid, db_path, "g/fails", status="approved")
    monkeypatch.setattr(gmb, "is_connected", lambda r: True)
    monkeypatch.setattr(gmb, "post_reply", lambda *a, **k: {"ok": False, "error": "Token expired."})
    posted, err = client_api._attempt_google_post(a, rid)
    assert posted is False and err == "Token expired."
    row = {r["id"]: r for r in models.get_reviews_data(rid)}[a]
    assert row["post_error"] == "Token expired." and models.review_post_failed(row, True) is True


def test_approved_before_connect_reads_post_it_once_connected():
    card = _read("templates/_review_card.html")
    assert "Post it once Google is connected" in card and "Will post to Google once connected" not in card
    assert "It was approved before Google was connected" in card


# ── #7 Read now only for logins the route accepts ───────────────────────────

def test_can_sync_matches_the_sync_routes_gate(rid):
    owner, _ = client_api._do_web_analytics_get(_user(rid))
    member, _ = client_api._do_web_analytics_get(dict(_user(rid), role="member"))
    assert owner["can_sync"] is True and member["can_sync"] is False
    payload, status = client_api._do_web_analytics_sync(dict(_user(rid), role="member"))
    assert status == 403


def test_the_web_hides_read_now_without_can_sync():
    dash = _read("templates/dashboard.html")
    render = dash[dash.index("function waRender(checks){"):dash.index("function waChecksHtml(checks){")]
    assert "&& d.can_sync ? '<button type=\"button\" class=\"cbtn cbtn-secondary cbtn-sm\" onclick=\"waSync(this)\">Read now" in render


# ── #8 Ask's review read ────────────────────────────────────────────────────

def test_ask_reads_the_posted_reply_not_the_unused_draft(rid, db_path):
    a = _review(rid, db_path, "g/elsewhere", draft="Our unused draft")
    assert models.mark_replied_elsewhere(a, rid, source="google", reply_text="Thanks Ann — Danny",
                                         replied_at="2026-10-03T12:00:00Z", db_path=db_path)
    out = ask_cavnar_tools._read_reviews(rid, review_id=a)
    r = out["reviews"][0]
    assert "draft" not in r and r["has_draft"] is False and r["replied_elsewhere"] is True
    assert r["reply_posted"] == "Thanks Ann — Danny"
    assert "Our unused draft" not in json.dumps(out)
    assert all(x["id"] != a for x in ask_cavnar_tools._read_reviews(rid, needs_response=True)["reviews"])


# ── #11 the website card under Intel ────────────────────────────────────────

def test_the_intel_website_route_is_gated_as_intel_and_shares_the_body():
    assert auth._required_module("/mobile/api/intel/website") == "intel"
    assert auth._required_module("/api/intel/website") == "intel"
    for src, fn in (("client_api.py", "def intel_website_api(current_user):"),
                    ("mobile_api.py", "def mobile_intel_website(current_user):")):
        body = _read(src)
        body = body[body.index(fn):]
        assert "_do_marketing_website(current_user)" in body[:body.index("return jsonify")]
    vm = _read("ios/CavnarAI/CavnarAI/Features/Intel/WebsiteAnalyticsViewModel.swift")
    assert 'summaryPath = "/mobile/api/intel/website"' in vm and '"/mobile/api/marketing/website"' not in vm
