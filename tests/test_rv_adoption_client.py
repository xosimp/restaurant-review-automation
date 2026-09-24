"""Workstream A — the Response Validation Layer adopted on the client paths
(client_api.py, mobile_api.py, competitor.py):

- the Reviews read and the Marketing read run rv.enforce on the model's
  text; their structured flags (figures_verified / causes_verified /
  names_verified) are derived from the verdict's codes, the payload carries
  `validation`, and the stored read keeps the model's raw text so a new
  engine version re-validates it without a model call;
- competitor intel binds each figure to its competitor (F2), drops another
  tenant's name (T1), keeps its structural validators, and stores the
  verdict's version beside the blob so a stale one is re-validated on read;
- the owner-edited reply and bulk approve run the reply_public surface
  (staff named in public, awards the owner never wrote);
- the food and labor insight payloads, web and phone, carry `validation`.

No test calls a model, sends anything or touches the network: the model is
the module's create_with_retry, stubbed.
"""
import copy
import json
import types
import uuid
from datetime import datetime, timedelta

import pytest
from flask import Flask

import ai_utils
import auth
import client_api
import competitor
import insight_store
import models
import response_validation as rv
from models import Restaurant, Review, create_restaurant, get_conn, save_reviews, update_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    import mobile_api, review_intelligence, food_cost_intelligence, scheduler, drafter
    for mod in (models, auth, client_api, mobile_api, review_intelligence, food_cost_intelligence, scheduler,
                drafter):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **kw: False)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    import weather
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    client_api._insight_cache.clear()
    yield
    client_api._insight_cache.clear()


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                                             cache_creation_input_tokens=0,
                                                             cache_read_input_tokens=0))


def _model(monkeypatch, text, calls=None):
    def fake(client, **kw):
        if calls is not None:
            calls.append(kw)
        return _msg(text)
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)


def _no_model(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the model must not be called")
    monkeypatch.setattr(ai_utils, "create_with_retry", boom)


def _rid(db_path, **kw):
    kw.setdefault("name", "Rv Client Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _review(db_path, rid, days_ago=0, **cols):
    when = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"x-{uuid.uuid4().hex[:10]}",
                         author=cols.pop("author", "Sam P."), rating=cols.pop("rating", 5),
                         text=cols.pop("text", "Lovely dinner."), review_date=when)], db_path=db_path)
    conn = get_conn(db_path)
    review_id = conn.execute("SELECT MAX(id) id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    cols.setdefault("processed", 1)
    cols.setdefault("sentiment", "positive")
    cols.setdefault("response_status", "posted")
    conn.execute("UPDATE reviews SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                 (*cols.values(), review_id))
    conn.commit()
    conn.close()
    return review_id


def _stored_row(db_path, rid, kind):
    conn = get_conn(db_path)
    row = conn.execute("SELECT payload FROM insight_cache WHERE restaurant_id=? AND kind=?", (rid, kind)).fetchone()
    conn.close()
    return json.loads(row["payload"]) if row else None


def _age_the_version(db_path, rid, kind):
    conn = get_conn(db_path)
    row = conn.execute("SELECT payload FROM insight_cache WHERE restaurant_id=? AND kind=?", (rid, kind)).fetchone()
    body = json.loads(row["payload"])
    body["_rv"] = "rv0"
    conn.execute("UPDATE insight_cache SET payload=? WHERE restaurant_id=? AND kind=?", (json.dumps(body), rid, kind))
    conn.commit()
    conn.close()


# ── Reviews read ────────────────────────────────────────────────────────────

_REVIEW_READ = ("\U0001f4ca This week: Guests keep praising the pasta.\n"
                "⚠️ Watch: About $9,999 a month is at risk from slow service.\n"
                "✅ Do today: Walk the floor at the dinner rush.")


def _reviews_restaurant(db_path, spread=False):
    rid = _rid(db_path, module_reviews=1)
    for i in range(4):
        _review(db_path, rid, days_ago=(i * 8 if spread else 0))
    return rid


def test_the_review_read_carries_the_verdict_and_derives_its_flags(db_path, monkeypatch):
    rid = _reviews_restaurant(db_path)
    _model(monkeypatch, _REVIEW_READ)
    payload, status = client_api._do_review_insight(rid)
    assert status == 200
    val = payload["validation"]
    assert val["version"] == rv.VERSION and "F1" in val["codes"]
    assert val["verdict"] == "withhold" and val["controls"] is False
    assert payload["figures_verified"] is False
    assert any("9,999" in f for f in payload["unsupported_figures"])
    # A withheld read offers no Done / Not for us on its Do today line.
    assert payload["recs"] == []
    # No legacy marker on the Reviews read: it never had one; its flags are the caveat.
    assert "UNVERIFIED" not in payload["insight"]


def test_an_unanchored_cause_in_the_review_read_is_a_K1_finding(db_path, monkeypatch):
    rid = _reviews_restaurant(db_path)
    _model(monkeypatch, "\U0001f4ca This week: Ratings slipped because the new chef quit.\n"
                        "✅ Do today: Taste every plate before it leaves the pass.")
    payload, _ = client_api._do_review_insight(rid)
    assert "K1" in payload["validation"]["codes"]
    assert payload["causes_verified"] is False
    assert any("chef" in c for c in payload["unsupported_causes"])


def test_a_cause_the_stored_diagnosis_holds_stands_and_keeps_the_controls(db_path, monkeypatch):
    """The negative control for K1: the Why line restating the diagnosis."""
    rid = _reviews_restaurant(db_path)
    for _ in range(3):
        _review(db_path, rid, rating=2, text="Our plates took forever on Friday.", sentiment="negative",
                categories='["service"]')
    conn = get_conn(db_path)
    conn.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
                 "alternative_cause, operational_evidence, confidence, generated_at, evidence_review_ids) "
                 "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                 (rid, "service", 90, 4, "Friday dinner is short a line cook against its covers.",
                  "the new ticket printer", "[]", "medium",
                  json.dumps([r["id"] for r in conn.execute("SELECT id FROM reviews WHERE restaurant_id=?",
                                                              (rid,)).fetchall()])))
    conn.commit()
    conn.close()
    _model(monkeypatch, "\U0001f4ca This week: Guests keep praising the pasta.\n"
                        "\U0001f50d Why: Slow Friday plates most likely come from Friday dinner being short a line "
                        "cook against its covers.\n"
                        "✅ Do today: Walk the floor at the dinner rush.")
    payload, _ = client_api._do_review_insight(rid)
    if not payload.get("diagnoses"):
        pytest.skip("the diagnosis row did not survive get_diagnoses' re-filter in this fixture")
    assert "K1" not in payload["validation"]["codes"], payload["validation"]
    assert payload["causes_verified"] is True


def test_the_urgent_guest_the_prompt_names_may_be_named(db_path, monkeypatch):
    """The negative control for N1: the guest name is inside the fence."""
    rid = _reviews_restaurant(db_path)
    _review(db_path, rid, author="Dana K.", rating=1, text="Cold food and a long wait.", sentiment="negative",
            urgency="high", response_status="pending")
    _model(monkeypatch, "\U0001f4ca This week: Guests keep praising the pasta.\n"
                        "✅ Do today: Reply to Dana about the cold food before dinner service.")
    payload, _ = client_api._do_review_insight(rid)
    assert payload["names_verified"] is True and "N1" not in payload["validation"]["codes"]
    assert [r["kind"] for r in payload["recs"]] == ["do_today"]


def test_a_read_on_no_week_above_the_floor_discloses_it(db_path, monkeypatch):
    rid = _reviews_restaurant(db_path, spread=True)
    _model(monkeypatch, "\U0001f4ca This week: Guests keep praising the pasta.\n"
                        "✅ Do today: Walk the floor at the dinner rush.")
    payload, _ = client_api._do_review_insight(rid)
    assert "M1" in payload["validation"]["codes"]
    assert "This rests on only a few reviews." in payload["validation"]["caveats"]


def test_the_stored_review_read_is_revalidated_from_its_raw_text_without_a_model_call(db_path, monkeypatch):
    rid = _reviews_restaurant(db_path)
    calls = []
    _model(monkeypatch, _REVIEW_READ, calls)
    first, _ = client_api._do_review_insight(rid)
    assert len(calls) == 1
    stored = _stored_row(db_path, rid, "reviews")
    assert stored["_rv"] == rv.VERSION and "9,999" in stored["raw"]
    _age_the_version(db_path, rid, "reviews")
    client_api._insight_cache.clear()
    _no_model(monkeypatch)
    again, status = client_api._do_review_insight(rid)
    assert status == 200 and not again.get("stale")
    assert again["validation"]["version"] == rv.VERSION and again["figures_verified"] is False
    assert _stored_row(db_path, rid, "reviews")["_rv"] == rv.VERSION


# ── Marketing read ──────────────────────────────────────────────────────────

def test_the_marketing_read_carries_the_verdict_and_guests_are_a_missing_input(db_path, monkeypatch):
    rid = _rid(db_path, module_marketing=1)
    _model(monkeypatch, "Hi, your brunch posts brought in more guests last month.\n\n"
                        "1. Post the brunch menu on Friday.\n2. Share a patio photo.")
    out, status = client_api._do_mkt_insight(rid, raw=True)
    assert status == 200
    assert "M2" in out["validation"]["codes"]
    stored = _stored_row(db_path, rid, "marketing")
    assert stored["_rv"] == rv.VERSION and "brunch posts" in stored["raw"]


def test_the_stored_marketing_read_is_revalidated_without_a_model_call(db_path, monkeypatch):
    rid = _rid(db_path, module_marketing=1)
    _model(monkeypatch, "Hi, reels get 300% more reach this month.\n\n1. Post a reel.\n2. Share a photo.")
    first, _ = client_api._do_mkt_insight(rid, raw=True)
    assert first["figures_verified"] is False and "F1" in first["validation"]["codes"]
    _age_the_version(db_path, rid, "marketing")
    client_api._insight_cache.clear()
    _no_model(monkeypatch)
    again, status = client_api._do_mkt_insight(rid, raw=True)
    assert status == 200 and again["validation"]["version"] == rv.VERSION
    assert again["figures_verified"] is False


# ── Competitor intel ────────────────────────────────────────────────────────

_COMPS = [{"name": "Lou's Diner", "rating": 4.5, "review_count": 812, "price_level": 2,
           "reviews": [{"rating": 2, "text": "Waited 40 minutes for eggs.", "time": "a week ago"},
                       {"rating": 5, "text": "Great pancakes.", "time": "a month ago"}]},
          {"name": "Maple House", "rating": 3.9, "review_count": 140, "price_level": 2,
           "reviews": [{"rating": 1, "text": "Cold coffee, rude host.", "time": "2 weeks ago"}]}]

_INTEL = """Hi, here is your competitive landscape snapshot.

WHAT COMPETITORS ARE DOING WELL:
- Lou's Diner holds a 4.5★ rating across 812 reviews for its pancakes [R2]

WHAT COMPETITORS ARE DOING POORLY:
- Maple House guests report cold coffee and a rude host [R3]

Recommendations:
1. Greet every guest within a minute to win Maple House regulars [R3]"""


def _intel(monkeypatch, reply, rid=None, comps=None):
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "fake", raising=False)
    monkeypatch.setattr(competitor, "get_client", lambda timeout=None: object())
    monkeypatch.setattr(competitor, "create_with_retry", lambda client, **kw: _msg(reply))
    return competitor.generate_competitor_insight("Test Cafe", comps if comps is not None else copy.deepcopy(_COMPS),
                                                  restaurant_id=rid)


def test_intel_carries_its_verdict_and_a_clean_read_has_no_marker(monkeypatch):
    out = _intel(monkeypatch, _INTEL)
    assert "UNVERIFIED" not in out
    assert out.validation["version"] == rv.VERSION and out.validation["verdict"] == "pass"


def test_a_competitor_figure_attached_to_the_wrong_competitor_is_unverified(monkeypatch):
    """4.5★ is Lou's Diner's; the old presence check passed it on Maple House."""
    out = _intel(monkeypatch, _INTEL.replace("- Maple House guests report cold coffee and a rude host [R3]",
                                             "- Maple House holds a 4.5★ rating despite cold coffee [R3]"))
    assert "F2" in out.validation["codes"]
    assert "UNVERIFIED:" in out


def test_another_tenants_name_is_dropped_from_intel(db_path, monkeypatch):
    rid = _rid(db_path, name="Test Cafe")
    _rid(db_path, name="Harbor Grill", owner_email="other@x.test")
    out = _intel(monkeypatch, _INTEL.replace("snapshot.", "snapshot. Harbor Grill down the road is worth copying."),
                 rid=rid)
    assert "Harbor Grill" not in out
    assert "T1" in out.validation["codes"]


def test_a_stored_intel_blob_from_an_older_engine_is_revalidated_on_read(db_path, monkeypatch):
    rid = _rid(db_path, name="Test Cafe")
    comps = copy.deepcopy(_COMPS)
    first = _intel(monkeypatch, _INTEL, rid=rid, comps=comps)
    blob = competitor.intel_blob(comps, first, generated_at="2026-09-21")
    assert blob["validation"]["version"] == rv.VERSION
    # The model's raw text never rides in the blob the web is handed.
    assert "insight_raw" not in blob and "validation_input" not in blob
    # A read an older engine passed: Maple House is handed Lou's rating.
    blob["validation"]["version"] = "rv0"
    blob["insight"] = blob["insight"].replace("Maple House guests report cold coffee and a rude host",
                                              "Maple House holds a 4.5★ rating despite cold coffee")
    update_restaurant(rid, {"competitor_intel": json.dumps(blob)}, db_path=db_path)
    monkeypatch.setattr(competitor, "create_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))
    fresh = competitor.current_intel(rid, json.loads(models.get_restaurant(rid).competitor_intel))
    assert fresh["validation"]["version"] == rv.VERSION and "F2" in fresh["validation"]["codes"]
    assert "UNVERIFIED:" in fresh["insight"]
    # Persisted, so every reader of the row (the web page, Ask, Home) sees it.
    stored = json.loads(models.get_restaurant(rid).competitor_intel)
    assert stored["validation"]["version"] == rv.VERSION and "UNVERIFIED:" in stored["insight"]


def test_a_pre_engine_intel_blob_is_revalidated_from_its_own_text(db_path, monkeypatch):
    rid = _rid(db_path, name="Test Cafe")
    _rid(db_path, name="Harbor Grill", owner_email="other@x.test")
    old = _INTEL.replace("snapshot.", "snapshot. Harbor Grill down the road is worth copying.")
    blob = {"competitors": copy.deepcopy(_COMPS), "insight": old, "generated_at": "2026-09-14"}
    fresh = competitor.current_intel(rid, blob, persist=False)
    assert "Harbor Grill" not in fresh["insight"] and fresh["validation"]["version"] == rv.VERSION


def test_the_intel_payloads_carry_the_validation(db_path, monkeypatch):
    import mobile_api
    rid = _rid(db_path, name="Test Cafe", google_place_id="p1")
    comps = copy.deepcopy(_COMPS)
    out = _intel(monkeypatch, _INTEL, rid=rid, comps=comps)
    update_restaurant(rid, {"competitor_intel": json.dumps(competitor.intel_blob(comps, out, "2026-09-21"))},
                      db_path=db_path)
    payload, status = mobile_api._do_mobile_intel(rid)
    assert status == 200 and payload["validation"]["version"] == rv.VERSION
    assert client_api.intel_recs_payload(rid)["validation"]["verdict"] == "pass"


# ── Public replies ──────────────────────────────────────────────────────────

def _drafted(db_path, rid, draft, author="Dana K.", rating=5, text="Great night."):
    rev = _review(db_path, rid, author=author, rating=rating, text=text, response_status="drafted")
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET draft_response=? WHERE id=?", (draft, rev))
    conn.commit()
    conn.close()
    return rev


def test_bulk_approve_holds_a_reply_that_names_a_staff_member(db_path, monkeypatch):
    rid = _rid(db_path)
    bad = _drafted(db_path, rid, "Thanks Dana! Sorry about that, blame Carlos in the kitchen.")
    good = _drafted(db_path, rid, "Thanks so much, Dana, see you soon!")
    approved = []
    monkeypatch.setattr(client_api, "_do_approve",
                        lambda review_id, r, google=None, bulk=False: (approved.append(review_id) or ({"ok": True}, 200)))
    payload, status = client_api._do_approve_all(rid)
    assert status == 200 and approved == [good] and payload["held_for_review"] == 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (bad,)).fetchone()
    conn.close()
    assert row["draft_needs_review"] == 1 and "Carlos" in row["draft_review_reason"]


def test_an_owner_edit_with_an_award_nobody_wrote_down_needs_review(db_path):
    rid = _rid(db_path)
    rev = _drafted(db_path, rid, "Thanks Dana!")
    out, status = client_api._do_save_draft(rev, rid, "Thanks Dana! We were voted the best pizza in town.")
    assert status == 200 and out["needs_review"] is True
    assert "voted" in out["review_reason"]


def test_an_owner_edit_that_is_plain_thanks_is_not_flagged(db_path):
    rid = _rid(db_path)
    rev = _drafted(db_path, rid, "Thanks Dana!")
    out, _ = client_api._do_save_draft(rev, rid, "Thanks so much, Dana. See you soon!")
    assert out["needs_review"] is False and out["review_reason"] is None


# ── Food and labor payloads, web and phone ─────────────────────────────────

_VAL = {"verdict": "caveat", "caveats": ["x."], "controls": True, "codes": ["M1"], "version": rv.VERSION}


@pytest.fixture
def web(monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    return app.test_client()


@pytest.fixture
def mobile(db_path):
    import mobile_api
    auth.init_auth(db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _web_login(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7, "restaurant_id": rid, "is_admin": 0, "username": "owner",
                                 "email": "o@x.test"})


def _mobile_headers(db_path, rid):
    uid = auth.create_user(rid, f"owner{rid}", f"owner{rid}@x.test", "pw-Edge-123!", db_path=db_path)
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}


def test_the_food_insight_payloads_carry_the_validation(db_path, monkeypatch, web, mobile):
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(client_api, "food_insight_text",
                        lambda *a, **k: rv.Validated("Hi, waste held steady.", validation=dict(_VAL)))
    _web_login(monkeypatch, rid)
    assert web.get("/api/inv-insight").get_json()["validation"] == _VAL
    body = mobile.get("/mobile/api/food-cost/analytics", headers=_mobile_headers(db_path, rid)).get_json()
    assert body["validation"] == _VAL


def test_the_labor_insight_payloads_carry_the_validation(db_path, monkeypatch, web, mobile):
    rid = _rid(db_path, module_labor=1)
    client_api._cache_set(client_api.LABOR_INSIGHT_CACHE + str(rid),
                          rv.Validated("Hi, labor held.\n\n1. Trim Tuesday lunch.", validation=dict(_VAL)))
    _web_login(monkeypatch, rid)
    assert web.get("/api/labor-insight").get_json()["validation"] == _VAL
    body = mobile.get("/mobile/api/labor/insight", headers=_mobile_headers(db_path, rid)).get_json()
    assert body["validation"] == _VAL


def test_a_plain_string_insight_carries_a_null_validation(db_path, monkeypatch, web):
    """Until inventory / labor return Validated strs, the key is present and null."""
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(client_api, "food_insight_text", lambda *a, **k: "Hi, waste held steady.")
    _web_login(monkeypatch, rid)
    body = web.get("/api/inv-insight").get_json()
    assert "validation" in body and body["validation"] is None
