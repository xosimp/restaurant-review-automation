"""Web/iOS parity round: Reviews, Marketing and Intel.

Each test names the parity finding it pins. Backend behaviour is tested
through the real routes (web client_bp and mobile mobile_bp); the client
halves are pinned against the template and Swift source, because the rule
must hold in every build, not only for one rendered fixture.
No model, network, email, SMS or push is reached.
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _swift(*parts):
    with open(os.path.join(IOS, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    import marketing_drafts
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    # marketing_drafts binds get_conn and DB_PATH at import, so a draft
    # written through it lands in whatever database was first imported
    # unless its copy is redirected too.
    monkeypatch.setattr(marketing_drafts, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    client_api._insight_cache.clear()
    yield
    client_api._insight_cache.clear()


@pytest.fixture
def rid(db_path):
    auth.init_auth(db_path=db_path)
    return create_restaurant(Restaurant(name="Gia Mia", owner_email="o@x.test"), db_path=db_path)


@pytest.fixture
def owner(rid, db_path):
    """A real login row, so anything keyed to the user (a draft's
    created_by) satisfies its foreign key."""
    uid = auth.create_user(rid, "owner", "owner@x.test", "correct-horse-battery", db_path=db_path)
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "username": "owner"}


@pytest.fixture
def web(owner, monkeypatch):
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(owner))
    return app.test_client()


@pytest.fixture
def phone(owner, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    monkeypatch.setattr(auth, "get_session_user",
                        lambda token: dict(owner) if token == "t" else None)
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer t"
    return client


_n = [0]


def _review(db_path, rid, *, draft="Thanks so much!", flagged=0, reason=None, author="Ann",
            text="Nice night", days_ago=1):
    _n[0] += 1
    c = sqlite3.connect(db_path)
    fields = {"restaurant_id": rid, "platform": "google", "external_id": f"p{_n[0]}", "author": author,
              "rating": 4, "text": text, "processed": 1, "response_status": "drafted",
              "draft_response": draft, "urgency": "normal", "draft_needs_review": flagged,
              "draft_review_reason": reason,
              "review_date": (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S"),
              "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
    rv = c.execute(f"INSERT INTO reviews ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                   tuple(fields.values())).lastrowid
    c.commit()
    c.close()
    return rv


def _status(db_path, rv):
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT response_status FROM reviews WHERE id=?", (rv,)).fetchone()[0]
    finally:
        c.close()


# ── 1. A flagged draft is never approved without the owner's confirm ────────

@pytest.mark.parametrize("surface", ["web", "phone"])
def test_1_a_flagged_draft_is_refused_until_the_owner_confirms(surface, request, db_path, rid):
    client = request.getfixturevalue(surface)
    path = "/approve/{}" if surface == "web" else "/mobile/api/reviews/{}/approve"
    rv = _review(db_path, rid, flagged=1, reason="promises a free dinner")

    refused = client.post(path.format(rv))                     # a swipe, a lock-screen action, Ask
    assert refused.status_code == 409
    body = refused.get_json()
    assert body["ok"] is False and body["needs_review"] is True
    assert body["review_reason"] == "promises a free dinner"
    assert "promises a free dinner" in body["error"]
    assert _status(db_path, rv) == "drafted", "nothing was posted"

    # Only an explicit JSON true counts as the confirm.
    assert client.post(path.format(rv), json={"confirm_flagged": "yes"}).status_code == 409
    ok = client.post(path.format(rv), json={"confirm_flagged": True})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    assert _status(db_path, rv) == "approved"


@pytest.mark.parametrize("surface", ["web", "phone"])
def test_1_a_clean_draft_needs_no_confirm(surface, request, db_path, rid):
    client = request.getfixturevalue(surface)
    path = "/approve/{}" if surface == "web" else "/mobile/api/reviews/{}/approve"
    rv = _review(db_path, rid)
    assert client.post(path.format(rv)).status_code == 200
    assert _status(db_path, rv) == "approved"


def test_1_the_flag_is_held_in_the_claim_itself(db_path, rid):
    """The compare-and-set, not a read before it: a draft flagged between
    the screen's read and the approve is not claimed."""
    rv = _review(db_path, rid, flagged=1, reason="x")
    assert models.claim_approval(rv, rid, allow_flagged=False) is False
    assert _status(db_path, rv) == "drafted"
    assert models.claim_approval(rv, rid) is True


def test_1_the_scheduler_rule_never_posts_a_flagged_draft(db_path, rid):
    rv = _review(db_path, rid, flagged=1, reason="x")
    payload, status = client_api._do_approve(rv, rid, auto=True)
    assert status == 409 and payload.get("needs_review") is True
    assert _status(db_path, rv) == "drafted"


def test_1_web_sends_the_confirm_only_after_asking():
    src = _read("templates", "dashboard.html")
    start = src.index("function approveR(id, confirmed)")
    body = src[start:src.index("function rvReloadInbox", start)]
    assert "_revFlagOk(id,_flag)" in body
    assert "confirm_flagged:_conf" in body
    # A refusal for a flag the card didn't know about shows it and asks.
    assert "d.needs_review&&!_conf" in body and "_revShowFlag(id," in body
    save = src[src.index("function saveDraft(id)"):]
    save = save[:save.index("var _igConnected")]
    assert "confirm_flagged:!!sd.needs_review" in save


def test_1_ios_follows_the_flag_and_confirms_before_posting():
    vm = _swift("Reviews", "ReviewDetailViewModel.swift")
    assert 'case needsReview = "needs_review"' in vm and 'case reviewReason = "review_reason"' in vm
    assert vm.count("applyFlag(response)") == 2, "regenerate and save both update the flag"
    assert 'case confirmFlagged = "confirm_flagged"' in vm
    assert "if flagReason != nil && !confirmFlagged" in vm
    assert "queueApprove(confirmFlagged: confirmFlagged)" in vm
    view = _swift("Reviews", "ReviewDetailView.swift")
    assert "if let reason = viewModel.flagReason" in view
    assert '"Post this reply anyway?"' in view
    assert "viewModel.approve(confirmFlagged: true)" in view
    lst = _swift("Reviews", "ReviewsListView.swift")
    assert "detail.needsFlagConfirm" in lst


# ── 2. iOS search reaches the whole inbox ───────────────────────────────────

def test_2_mobile_search_finds_a_review_past_the_first_page(phone, db_path, rid):
    for i in range(models.REVIEWS_PAGE_SIZE + 5):
        _review(db_path, rid, author=f"Guest {i}", text="Fine", days_ago=1)
    old = _review(db_path, rid, author="Priya", text="The risotto was cold", days_ago=300)
    first = phone.get("/mobile/api/reviews?limit=50&offset=0").get_json()
    assert old not in [r["id"] for r in first["reviews"]]
    found = phone.get("/mobile/api/reviews?limit=50&offset=0&search=risotto").get_json()
    assert [r["id"] for r in found["reviews"]] == [old] and found["total"] == 1


def test_2_ios_sends_the_search_with_paging():
    vm = _swift("Reviews", "ReviewsListViewModel.swift")
    assert 'query["search"] = search' in vm
    assert 'query["search"] = loadedSearch' in vm, "the next page keeps the same search"
    assert "Task.sleep(for: .milliseconds(350))" in vm, "debounced"


# ── 3. Re-saving an opened marketing draft updates it ───────────────────────

def test_3_saving_with_the_id_updates_the_draft_instead_of_copying_it(phone, rid):
    from marketing_drafts import list_drafts
    first = phone.post("/mobile/api/marketing/drafts", json={"body": "Tuesday pasta night", "topic": "Pasta"})
    assert first.status_code == 200
    draft_id = first.get_json()["id"]
    again = phone.post("/mobile/api/marketing/drafts",
                       json={"id": draft_id, "body": "Tuesday pasta night, 5-9pm", "topic": "Pasta"})
    assert again.get_json()["id"] == draft_id
    drafts = list_drafts(rid)
    assert len(drafts) == 1 and drafts[0]["body"] == "Tuesday pasta night, 5-9pm"


def test_3_web_sends_the_draft_id_and_restores_type_and_photo():
    src = _read("templates", "dashboard.html")
    save = src[src.index("function saveMktDraft()"):src.index("function loadMktDrafts()")]
    assert "payload.id = window._mktDraftId" in save
    use = src[src.index("function useMktDraft(id)"):src.index("function approveMktDraft(id)")]
    assert "window._mktDraftId = x.id" in use
    assert "selectCt(x.content_type, ctBtn)" in use
    assert "window._mktPhotoId = x.media_id" in use
    gen = src[src.index("function genContent(fromCalendar)"):]
    assert "window._mktDraftId=null" in gen[:3000], "a new generation is a new draft"


def test_3_ios_sends_the_opened_draft_id_and_photo():
    vm = _swift("Marketing", "MarketingComposeViewModel.swift")
    assert "DraftBody(id: savedDraftID," in vm
    assert "func open(_ draft: MarketingDraft)" in vm
    view = _swift("Marketing", "MarketingView.swift")
    assert "compose.open(draft)" in view
    assert "compose.savedDraftID = nil" in view


# ── 4. The web keeps an edited reply as it is typed ─────────────────────────

def test_4_web_autosaves_an_edited_draft_debounced_and_only_when_changed():
    src = _read("templates", "dashboard.html")
    auto = src[src.index("var _revAutoT = {};"):src.index("function saveDraft(id) {")]
    assert "setTimeout(function(){ _revAutoSave(id); }, 800)" in auto
    assert "fetch('/api/save-draft/'+id" in auto and "JSON.stringify({draft:draft})" in auto
    assert "draft === (tx.textContent || '').trim()" in auto, "an unchanged draft is not saved"
    assert "_revAutoStatus(id, 'Saved')" in auto
    assert "_revShowFlag(id, sd.needs_review" in auto, "the guard's answer updates the flag"
    regen = src[src.index("function regenDraft(id) {"):]
    assert "clearTimeout(_revAutoT[id])" in regen[:400], "a regenerate cancels a pending autosave"


# ── 5. One body per route twin ──────────────────────────────────────────────

def test_5a_generate_content_is_one_body(web, phone, monkeypatch):
    import marketing
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: "Pasta night is back.")
    for client, path in ((web, "/api/generate-content"), (phone, "/mobile/api/marketing/generate-content")):
        body = client.post(path, json={"type": "instagram_post", "topic": "pasta"}).get_json()
        assert body["ok"] is True and body["content"] == "Pasta night is back.", path
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: True)
    for client, path in ((web, "/api/generate-content"), (phone, "/mobile/api/marketing/generate-content")):
        resp = client.post(path, json={"type": "instagram_post", "topic": "pasta"})
        assert resp.status_code == 429 and resp.get_json()["ok"] is False, path
    assert not hasattr(mobile_api, "_do_mobile_generate_content")


def test_5a_the_web_never_types_a_failure_into_the_post_box():
    src = _read("templates", "dashboard.html")
    gen = src[src.index("function genContent(fromCalendar)"):src.index("function mktSendAsNewsletter()")]
    assert "if(!d.content){" in gen and "box.setAttribute('data-gen-error','1')" in gen
    body = src[src.index("function _mktBody(){"):]
    # The refusal also covers the "Posted" confirmation (web client audit #1).
    assert "if(el.getAttribute('data-gen-error') || el.getAttribute('data-mkt-posted')) return '';" in body[:300]


def test_5b_the_web_calendar_marks_an_idea_already_written_from(web, phone, rid, monkeypatch):
    import marketing
    week = [{"day": "Monday", "platform": "Instagram", "angle": "Truffle pasta", "type": "instagram_post"},
            {"day": "Tuesday", "platform": "Email", "angle": "Wine night", "type": "weekly_email"}]
    monkeypatch.setattr(marketing, "get_content_calendar_ideas", lambda **k: [dict(i) for i in week])
    monkeypatch.setattr(marketing, "get_cached_calendar", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "_week_start", lambda r: datetime(2000, 1, 3))
    c = models.get_conn()
    c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic) VALUES (?,?,?)",
              (rid, "calendar_instagram_post", "Truffle pasta"))
    c.commit()
    c.close()
    ideas = web.post("/api/content-calendar", json={"force": False}).get_json()["ideas"]
    assert [i["written"] for i in ideas] == [True, False]
    assert ideas[0]["answered"] is True, "the web grid renders 'Written from this idea' for it"
    cal = phone.post("/mobile/api/marketing/calendar").get_json()
    assert cal["ok"] is True and [i["written"] for i in cal["calendar"]] == [True, False]


def test_5c_post_to_google_says_the_same_thing_on_both(web, phone, monkeypatch):
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    for client, path in ((web, "/api/post-to-google"), (phone, "/mobile/api/marketing/google-post")):
        resp = client.post(path, json={"summary": "Oysters tonight"})
        assert resp.status_code == 409, path
        assert resp.get_json()["error"] == client_api.GOOGLE_NOT_CONNECTED
    assert "Settings → Connections" not in client_api.GOOGLE_NOT_CONNECTED


def test_5d_marketing_performance_is_one_body(web, phone):
    a = web.get("/api/mkt-performance").get_json()
    b = phone.get("/mobile/api/marketing/performance").get_json()
    assert a == b and a["ok"] is True
    import inspect
    assert "_capi._do_mkt_performance" in inspect.getsource(mobile_api.mobile_marketing_performance)


# ── 6. Ask about this on iOS, with the web's question and entity ────────────

def test_6_ios_asks_about_a_review_and_an_intel_rec_like_the_web():
    web = _read("templates", "dashboard.html")
    review_q = "About this review: what is the guest really saying, and is my reply right?"
    assert review_q in web
    detail = _swift("Reviews", "ReviewDetailView.swift")
    assert review_q in detail
    assert 'AskScreen(panel: "reviews", entityType: "review", entityId: "\\(viewModel.review.id)")' in detail
    assert "'About this Intel recommendation: '+r.text" in web and "data-ask-rec=\"'+recEsc(r.key)" in web
    intel = _swift("Intel", "IntelView.swift")
    assert 'question: "About this Intel recommendation: \\(rec.text)"' in intel
    assert 'AskScreen(panel: "competitor", entityType: "rec", entityId: key)' in intel


# ── 7. Competitor movement is shown on both Intel screens ───────────────────

def test_7_movement_route_is_one_body_and_says_when_it_cannot_compare(web, phone):
    a = web.get("/api/intel/movement").get_json()
    b = phone.get("/mobile/api/intel/movement").get_json()
    assert a == b and a["ok"] is True
    assert a["compared_from"] is None and a["arrived"] == [] and a["gone"] == []


def test_7_both_clients_render_what_changed_with_an_honest_empty_state():
    src = _read("templates", "dashboard.html")
    fn = src[src.index("function in2LoadMoves(){"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "fetch('/api/intel/movement'" in fn
    assert "two weekly checks to compare" in fn, "no comparison yet is not 'no change'"
    assert "mdy(d.compared_from)" in fn, "dates read M/D/YY"
    assert 'id="in2-moves"' in src and "in2LoadMoves()" in src[src.index("if(n==='competitor')"):][:400]
    vm = _swift("Intel", "IntelViewModel.swift")
    assert '"/mobile/api/intel/movement"' in vm
    view = _swift("Intel", "IntelView.swift")
    assert "movementSection(movement)" in view
    assert "two weekly checks to compare" in view and "CavnarDate.mdy(from)" in view


# ── 8. Smaller parity fixes ─────────────────────────────────────────────────

def test_8a_ios_stopping_a_competitor_has_an_undo_window():
    view = _swift("Intel", "IntelView.swift")
    stop = view[view.index("private func stopTracking(_ c: Competitor)"):]
    stop = stop[:stop.index("private func undoRemoval()")]
    assert "Task.sleep(for: .seconds(7))" in stop
    assert stop.index("Task.sleep") < stop.index("viewModel.removeCompetitor(placeId: c.placeId)"), \
        "the server call waits for the Undo window"
    assert 'Text("Undo")' in view and ".onDisappear { undoRemoval() }" in view
    assert "Undo toast (iOS)" in _read("DESIGN_SYSTEM.md")


def test_8b_ios_saves_and_deletes_review_templates(phone, rid):
    made = phone.post("/mobile/api/templates", json={"title": "Five star", "body": "Thanks so much!"}).get_json()
    assert made["ok"] is True
    assert [t["title"] for t in phone.get("/mobile/api/templates").get_json()["templates"]] == ["Five star"]
    assert phone.delete(f"/mobile/api/templates/{made['id']}").get_json()["ok"] is True
    assert phone.get("/mobile/api/templates").get_json()["templates"] == []
    vm = _swift("Reviews", "ReviewDetailViewModel.swift")
    assert '"/mobile/api/templates", method: .post' in vm
    assert '"/mobile/api/templates/\\(template.id)", method: .delete' in vm
    assert 'Button("Save as template")' in _swift("Reviews", "ReviewDetailView.swift")


def test_8c_review_request_message_on_web_and_stats_on_ios():
    src = _read("templates", "dashboard.html")
    assert 'id="rr-message" maxlength="200"' in src
    send = src[src.index("fetch('/api/send-review-request'"):][:400]
    assert "message: (document.getElementById('rr-message')" in send
    sheet = _swift("Reviews", "SendReviewRequestSheet.swift")
    assert '"/mobile/api/review-request-stats"' in sheet and "Sent this month:" in sheet


def test_8d_ios_guest_campaign_send_carries_the_type():
    vm = _swift("Marketing", "GuestTextClubViewModel.swift")
    assert "SendBody(message: draftMessage, segment: selectedSegment, type: campaignType," in vm


def test_8e_ios_renders_the_guest_diagnosis(phone):
    import strategy_routes
    phone.application.register_blueprint(strategy_routes.strategy_mobile_bp)
    body = phone.get("/mobile/api/marketing/diagnosis").get_json()
    assert body["ok"] is True and "diagnosis" in body
    vm = _swift("Marketing", "MarketingAnalyticsViewModel.swift")
    assert '"/mobile/api/marketing/diagnosis"' in vm
    assert 'LaborDiagnosisCard(diagnosis: diagnosis, title: "WHY SOME TEXTS DID BETTER", surface: "marketing")' \
        in _swift("Marketing", "MarketingAnalyticsSection.swift")


def test_8f_one_name_per_concept_on_both_platforms():
    ios = "".join(_swift(*p) for p in (("Reviews", "ResponseRingsChart.swift"),
                                       ("Reviews", "TopicHeatGridChart.swift"),
                                       ("Reviews", "SentimentRiverChart.swift"),
                                       ("Intel", "AIVisibilitySection.swift")))
    for old in ('"Response Rings"', '"Topic Heat Grid"', '"Sentiment River"', "GBP completeness",
                "GBP COMPLETENESS", "YOUR AI VISIBILITY ROADMAP"):
        assert old not in ios, old
    for name in ("How your replies were approved", "What guests talk about", "Reviews · per week",
                 "LISTING STRENGTH", "GAPS IN YOUR PUBLIC RECORD"):
        assert name in ios, name
    assert "GBP completeness" not in _read("templates", "dashboard.html")


def test_8g_web_drafts_list_names_who_wrote_and_approved():
    src = _read("templates", "dashboard.html")
    by = src[src.index("function _mktDraftByline(x){"):src.index("function useMktDraft(id){")]
    assert "'Written by ' + _escHtml(x.created_by_name)" in by
    assert "'approved by ' + _escHtml(x.approved_by_name)" in by
    assert "+ _mktDraftByline(x)" in src
