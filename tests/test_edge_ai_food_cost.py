"""Edge cases in Food Cost's AI surfaces: invoice auto-apply, the recipe
photo reader, and the insight routes when the AI budget has stopped spending.

What these protect:

* A trusted supplier's invoice is applied without the owner looking, so
  every line that skips that look must still be one the code could check.
  A line for an ingredient with no current cost (nothing to compare the
  model's reading against) or a line whose quantity x price = total check
  could not run is exactly where a misread digit gets through (AI-19).
* A recipe card that is too long to read in one go is a different problem
  from a blurry photo, and the owner is told which one it is (AI-26).
* A budget stop is a deliberate pause. The Food Cost and Labor insight
  routes, web and mobile, must say "paused" rather than "check back
  shortly", which the owner retries forever (AI-11).

Nothing here reaches Anthropic: the model is a fake client or a forced
budget stop that raises before any client is touched.
"""
import json
from types import SimpleNamespace

import pytest
from flask import Flask

import ai_utils
import auth
import models
# Imported at module scope on purpose: each binds `from models import
# get_conn` at import, and a module first imported inside a patched test
# would keep that test's database for the rest of the session.
import client_api
import inventory
import inventory_ledger
import invoices
import labor
import mobile_api
import ordering
import recipes
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    # invoices, ordering and recipes bind get_conn at import (CLAUDE.md's
    # "Bound imports"); client_api and mobile_api resolve through models.
    for mod in (models, auth, invoices, ordering, recipes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    # Budget checks read the ledger; they are forced explicitly where a test
    # is about the budget, and off everywhere else.
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_record_budget_stop", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    # Insight caches are process-global and keyed by restaurant id, which
    # every fresh test database hands out again from 1.
    monkeypatch.setattr(client_api, "_insight_cache", {})


def _rid(db_path, **kw):
    kw.setdefault("module_inventory", 1)
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Edge Kitchen"),
                                        owner_email="o@x.test", **kw), db_path=db_path)


def _ingredient(db_path, rid, name, unit, cost):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, case_size, is_active) "
                       "VALUES (?,?,?,?,1,1)", (rid, name, unit, cost))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


def _cost(db_path, iid):
    conn = get_conn(db_path)
    v = conn.execute("SELECT unit_cost FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
    conn.close()
    return v


def _message(text, stop_reason="end_turn"):
    """A stand-in for anthropic's Message: content blocks, stop reason, usage."""
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=900, output_tokens=400,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
        model="claude-sonnet-test")


class _FakeClient:
    """anthropic.Anthropic in miniature: records each request, returns one reply."""

    def __init__(self, text, stop_reason="end_turn"):
        self.requests = []
        self._text, self._stop = text, stop_reason
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        return _message(self._text, self._stop)


# ── AI-19: what a trusted supplier's scan may apply without the owner ──────

def _earn_trust(db_path, rid, supplier, ingredient_id):
    """Three past scans from this supplier, each applied with every
    preselected line accepted — the record ordering.invoice_trust reads."""
    lines = [{"index": 0, "ingredient_id": ingredient_id, "proposed_cost": 21.0,
              "selected": True, "note": None}]
    conn = get_conn(db_path)
    for i in range(3):
        conn.execute("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, image_sha, "
                     "lines_json, applied_json, applied_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                     (rid, supplier, "2026-09-0%d" % (i + 1), "history-%d" % i,
                      json.dumps({"lines": lines}), json.dumps([{"index": 0}])))
    conn.commit()
    conn.close()
    assert ordering.invoice_trust(rid, supplier, db_path=db_path)["trusted"] is True


def _scan_and_auto_apply(db_path, rid, lines, supplier="Fresh Co", sha=b"invoice-photo"):
    """The invoice-scan route's own sequence: scan, then auto-apply if trusted."""
    fake = _FakeClient(json.dumps({"supplier": supplier, "invoice_date": "2026-09-20",
                                   "invoice_total": None, "lines": lines}))
    proposal = invoices.scan(rid, b"\x89PNG " + sha, "image/png", user_id=None,
                             db_path=db_path, client=fake)
    return ordering.auto_apply_if_trusted(rid, proposal, db_path=db_path)


def test_a_trusted_suppliers_clean_line_is_still_applied_on_scan(db_path):
    """The control: the automation the exclusions below must not break."""
    rid = _rid(db_path)
    romaine = _ingredient(db_path, rid, "Romaine Hearts", "case", 20.0)
    _earn_trust(db_path, rid, "Fresh Co", romaine)
    out = _scan_and_auto_apply(db_path, rid, [
        {"description": "ROMAINE HEARTS 24CT", "quantity": 2, "unit": "CS",
         "unit_price": 22.5, "line_total": 45.0}])
    assert [a["ingredient_id"] for a in out["auto_applied"]] == [romaine]
    assert _cost(db_path, romaine) == 22.5


def test_a_trusted_suppliers_line_for_an_ingredient_with_no_current_cost_waits_for_the_owner(db_path):
    """Nothing to compare the reading against: $189 read for $1.89 goes
    straight into plate costs unless a person looks first."""
    rid = _rid(db_path)
    romaine = _ingredient(db_path, rid, "Romaine Hearts", "case", 20.0)
    saffron = _ingredient(db_path, rid, "Saffron Threads", "oz", 0.0)
    _earn_trust(db_path, rid, "Fresh Co", romaine)
    out = _scan_and_auto_apply(db_path, rid, [
        {"description": "SAFFRON THREADS SPANISH", "quantity": 1, "unit": "OZ",
         "unit_price": 189.0, "line_total": 189.0}])
    assert saffron not in [a["ingredient_id"] for a in out["auto_applied"]]
    assert _cost(db_path, saffron) == 0.0


def test_a_trusted_suppliers_line_for_an_ingredient_with_a_null_cost_waits_for_the_owner(db_path):
    rid = _rid(db_path)
    romaine = _ingredient(db_path, rid, "Romaine Hearts", "case", 20.0)
    basil = _ingredient(db_path, rid, "Genovese Basil", "lb", None)
    _earn_trust(db_path, rid, "Fresh Co", romaine)
    out = _scan_and_auto_apply(db_path, rid, [
        {"description": "GENOVESE BASIL FRESH", "quantity": 2, "unit": "LB",
         "unit_price": 140.0, "line_total": 280.0}])
    assert basil not in [a["ingredient_id"] for a in out["auto_applied"]]
    assert _cost(db_path, basil) is None


def test_a_trusted_suppliers_line_with_no_quantity_read_waits_for_the_owner(db_path):
    """quantity x price = total is the only arithmetic check on the reading;
    a line where it could not run has had no check at all."""
    rid = _rid(db_path)
    romaine = _ingredient(db_path, rid, "Romaine Hearts", "case", 20.0)
    butter = _ingredient(db_path, rid, "Butter Unsalted", "lb", 4.0)
    _earn_trust(db_path, rid, "Fresh Co", romaine)
    line = {"description": "BUTTER UNSALTED AA", "quantity": None, "unit": "LB",
            "unit_price": 4.4, "line_total": 88.0}
    assert invoices._adds_up(line) is None
    out = _scan_and_auto_apply(db_path, rid, [line])
    assert butter not in [a["ingredient_id"] for a in out["auto_applied"]]
    assert _cost(db_path, butter) == 4.0


def test_a_trusted_suppliers_line_with_no_line_total_read_waits_for_the_owner(db_path):
    rid = _rid(db_path)
    romaine = _ingredient(db_path, rid, "Romaine Hearts", "case", 20.0)
    butter = _ingredient(db_path, rid, "Butter Unsalted", "lb", 4.0)
    _earn_trust(db_path, rid, "Fresh Co", romaine)
    line = {"description": "BUTTER UNSALTED AA", "quantity": 20, "unit": "LB",
            "unit_price": 4.4, "line_total": None}
    assert invoices._adds_up(line) is None
    out = _scan_and_auto_apply(db_path, rid, [line])
    assert butter not in [a["ingredient_id"] for a in out["auto_applied"]]
    assert _cost(db_path, butter) == 4.0


def test_an_untrusted_suppliers_zero_cost_line_is_only_ever_proposed(db_path):
    """Without the trust record nothing is written on scan, whatever the line."""
    rid = _rid(db_path)
    saffron = _ingredient(db_path, rid, "Saffron Threads", "oz", 0.0)
    out = _scan_and_auto_apply(db_path, rid, [
        {"description": "SAFFRON THREADS SPANISH", "quantity": 1, "unit": "OZ",
         "unit_price": 189.0, "line_total": 189.0}], supplier="New Supplier")
    assert out["trust"]["trusted"] is False and out["auto_applied"] == []
    assert _cost(db_path, saffron) == 0.0


# ── AI-26: a recipe card too long to read is not a blurry photo ──────────────

def _recipe_setup(db_path):
    rid = _rid(db_path)
    _ingredient(db_path, rid, "Mozzarella", "lb", 5.0)
    _ingredient(db_path, rid, "Flour", "lb", 0.6)
    return rid


def _drafts(db_path, rid):
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM recipe_drafts WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    return n


_TRUNCATED_CARD = ('{"menu_item_name": "Margherita Pizza", "note": null, "ingredients": ['
                   '{"name": "Mozzarella", "qty": 0.25, "unit": "lb", "confidence": "high"}, '
                   '{"name": "Flour", "qty": 0.4, "unit": "lb", "confid')


def test_a_recipe_card_cut_off_at_the_token_limit_says_it_was_too_long_not_try_a_clearer_photo(db_path):
    rid = _recipe_setup(db_path)
    fake = _FakeClient(_TRUNCATED_CARD, stop_reason="max_tokens")
    with pytest.raises(recipes.RecipePhotoError) as err:
        recipes.extract_from_image(rid, b"\xff\xd8 long card", "image/jpeg", client=fake, db_path=db_path)
    msg = str(err.value).lower()
    assert "clearer photo" not in msg
    assert any(w in msg for w in ("too long", "too many", "at a time", "truncat", "cut off")), msg


def test_a_recipe_card_cut_off_at_the_token_limit_writes_no_draft(db_path):
    rid = _recipe_setup(db_path)
    fake = _FakeClient(_TRUNCATED_CARD, stop_reason="max_tokens")
    with pytest.raises(recipes.RecipePhotoError):
        recipes.extract_from_image(rid, b"\xff\xd8 long card", "image/jpeg", client=fake, db_path=db_path)
    assert _drafts(db_path, rid) == 0


def test_a_refused_recipe_photo_still_asks_for_a_clearer_photo(db_path):
    """The message the truncation case must stop borrowing belongs here."""
    rid = _recipe_setup(db_path)
    fake = _FakeClient("", stop_reason="refusal")
    with pytest.raises(recipes.RecipePhotoError) as err:
        recipes.extract_from_image(rid, b"\xff\xd8 card", "image/jpeg", client=fake, db_path=db_path)
    assert "clearer photo" in str(err.value)
    assert _drafts(db_path, rid) == 0


def test_a_recipe_photo_under_a_budget_stop_raises_the_paused_message_before_any_call(db_path, monkeypatch):
    rid = _recipe_setup(db_path)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "monthly AI budget")
    fake = _FakeClient(_TRUNCATED_CARD)
    with pytest.raises(ai_utils.AIBudgetExceeded) as err:
        recipes.extract_from_image(rid, b"\xff\xd8 card", "image/jpeg", client=fake, db_path=db_path)
    assert "paused" in str(err.value) and fake.requests == []


# ── AI-11: a budget stop on the insight routes says "paused" ─────────────────

class _Unreachable:
    """A client that fails the test if the budget stop let a call through."""
    @property
    def messages(self):
        raise AssertionError("a budget-stopped call reached the client")


def _budget_stopped_model_call(*_a, **kw):
    """Stands in for the insight function's own create_with_retry call —
    the real one, under a forced budget stop, so the exception and its
    sentence are exactly what production raises."""
    return ai_utils.create_with_retry(_Unreachable(), restaurant_id=kw.get("restaurant_id"),
                                      action="insight", model="m", max_tokens=10,
                                      messages=[{"role": "user", "content": "x"}])


@pytest.fixture
def budget_stopped(monkeypatch):
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "monthly AI budget")
    monkeypatch.setattr(inventory, "analysis_for", lambda rid: ([], True, {"total_items": 3}))
    monkeypatch.setattr(inventory, "get_claude_insights", _budget_stopped_model_call)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {"total_sales": 1000})
    monkeypatch.setattr(labor, "get_claude_insights", _budget_stopped_model_call)


@pytest.fixture
def http(monkeypatch, db_path):
    rid = _rid(db_path)
    user = {"id": 7, "restaurant_id": rid, "is_admin": 0, "role": "owner",
            "username": "owner", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


_BEARER = {"Authorization": "Bearer t"}


def test_the_forced_budget_stop_raises_the_paused_sentence(budget_stopped):
    """Guards the fixture: the routes below are handed the real exception."""
    with pytest.raises(ai_utils.AIBudgetExceeded) as err:
        inventory.get_claude_insights({}, restaurant_id=1)
    assert "paused" in str(err.value)


def test_the_web_food_cost_insight_says_paused_when_the_ai_budget_has_stopped(http, budget_stopped):
    r = http.get("/api/inv-insight")
    body = r.get_json()
    assert "paused" in body["insight"], body
    assert "check back shortly" not in body["insight"]


def test_the_mobile_food_cost_insight_says_paused_when_the_ai_budget_has_stopped(http, budget_stopped):
    r = http.get("/mobile/api/food-cost/analytics", headers=_BEARER)
    body = r.get_json()
    assert "paused" in body["insight"], body
    assert "paused" in body["insight_intro"]
    assert "check back shortly" not in body["insight"]


def test_the_web_labor_insight_says_paused_when_the_ai_budget_has_stopped(http, budget_stopped):
    r = http.get("/api/labor-insight")
    body = r.get_json()
    assert "paused" in body["insight"], body
    assert "check back shortly" not in body["insight"]


def test_the_mobile_labor_insight_says_paused_when_the_ai_budget_has_stopped(http, budget_stopped):
    r = http.get("/mobile/api/labor/insight", headers=_BEARER)
    body = r.get_json()
    assert "paused" in body["insight"], body
    assert "paused" in body["insight_intro"]
    assert "check back shortly" not in body["insight"]


def test_an_ordinary_food_cost_insight_failure_still_says_check_back_shortly(http, monkeypatch):
    """The other side of the line: a transient failure is not a pause, and
    keeps the retry wording the budget case must stop using."""
    monkeypatch.setattr(inventory, "analysis_for", lambda rid: ([], True, {"total_items": 3}))

    def _boom(*a, **k):
        raise RuntimeError("upstream hiccup")
    monkeypatch.setattr(inventory, "get_claude_insights", _boom)
    r = http.get("/api/inv-insight")
    assert r.status_code == 500
    body = r.get_json()
    assert "check back shortly" in body["insight"] and "paused" not in body["insight"]
