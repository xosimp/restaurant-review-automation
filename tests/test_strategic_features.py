"""Audit #18 features on top of the foundations: invoice scanning, the HTTP
surface (web + mobile twins, permission lines, the public issue link), the
scheduled jobs, the staff pre-shift briefing and campaign outcome tracking.

As in test_strategic_foundations, most tests pin a refusal: a price that must
not be written, a login that must not see a number, a link preview that must
not acknowledge an issue.
"""
import json
from types import SimpleNamespace

import pytest
from flask import Flask

import auth
import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import metrics, outcomes, goals, menu_intelligence, demand, loss_detection, issues
    import invoices, strategy_jobs, review_intelligence, food_cost_intelligence
    import business_intelligence, morning_brief
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for m in (models, auth, metrics, outcomes, goals, menu_intelligence, demand, loss_detection,
              issues, invoices, strategy_jobs, review_intelligence, food_cost_intelligence,
              business_intelligence, morning_brief):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)


def _rid(db_path, name="Feat Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email="f@x.com", **kw), db_path=db_path)


def _ingredient(db_path, rid, name, unit="lb", cost=4.0, case_size=1.0):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, case_size) "
                       "VALUES (?,?,?,?,?)", (rid, name, unit, cost, case_size))
    conn.commit(); iid = cur.lastrowid; conn.close()
    return iid


def _cost(db_path, iid):
    conn = get_conn(db_path)
    v = conn.execute("SELECT unit_cost FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
    conn.close()
    return v


def _line(desc, qty, unit, price, total=None):
    return {"description": desc, "quantity": qty, "unit": unit, "unit_price": price,
            "line_total": round(qty * price, 2) if total is None else total}


# ── invoices: matching ─────────────────────────────────────────────────────

def test_an_ingredient_matches_only_when_every_word_of_its_name_is_on_the_line():
    import invoices
    ings = [{"id": 1, "name": "Chicken Breast"}, {"id": 2, "name": "Mozzarella Fresh"},
            {"id": 3, "name": "Chicken Thighs"}]
    assert invoices.match_ingredient("CHICKEN BREAST BNLS 4/10#", ings)["id"] == 1
    assert invoices.match_ingredient("CHICKEN THIGHS BNLS", ings)["id"] == 3
    # A shared word is not enough: whole-milk mozzarella is not fresh mozzarella.
    assert invoices.match_ingredient("MOZZARELLA WHOLE MILK", ings) is None
    assert invoices.match_ingredient("CHICKEN WINGS", ings) is None


def test_two_equally_specific_matches_are_no_match():
    import invoices
    ings = [{"id": 1, "name": "Basil"}, {"id": 2, "name": "Garlic"}]
    assert invoices.match_ingredient("BASIL GARLIC PESTO", ings) is None


# ── invoices: proposals ────────────────────────────────────────────────────

def test_a_clean_line_in_the_same_unit_is_proposed_and_preselected(db_path):
    import invoices
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, "Parmesan Cheese", unit="lb", cost=10.0)
    p = invoices.propose(rid, {"lines": [_line("PARMESAN CHEESE GRATED", 5, "LB", 11.0)]})
    ln = p["lines"][0]
    assert ln["ingredient_id"] == iid and ln["proposed_cost"] == 11.0
    assert ln["change_pct"] == 10.0 and ln["selected"] is True


def test_a_case_price_is_divided_by_the_ingredients_case_size(db_path):
    import invoices
    rid = _rid(db_path)
    _ingredient(db_path, rid, "Heavy Cream", unit="qt", cost=3.0, case_size=12)
    ln = invoices.propose(rid, {"lines": [_line("HEAVY CREAM 12/1QT", 1, "CS", 38.40)]})["lines"][0]
    assert ln["proposed_cost"] == 3.2 and ln["selected"] is True


def test_mismatched_units_get_no_proposal(db_path):
    import invoices
    rid = _rid(db_path)
    _ingredient(db_path, rid, "Garlic", unit="lb", cost=3.0)
    ln = invoices.propose(rid, {"lines": [_line("GARLIC PEELED", 2, "GAL", 12.0)]})["lines"][0]
    assert ln["proposed_cost"] is None and ln["selected"] is False and "enter the cost" in ln["note"]


def test_a_line_that_doesnt_add_up_is_never_preselected(db_path):
    """qty × price ≠ total means a digit was misread somewhere."""
    import invoices
    rid = _rid(db_path)
    _ingredient(db_path, rid, "Butter Unsalted", unit="lb", cost=4.0)
    ln = invoices.propose(rid, {"lines": [_line("BUTTER UNSALTED", 36, "LB", 4.2, total=15.12)]})["lines"][0]
    assert ln["selected"] is False and ln["proposed_cost"] is None
    assert "doesn't match the line total" in ln["note"]


def test_a_big_jump_is_shown_but_not_preselected(db_path):
    """A 4/10# case costed as a 10-count is the usual cause of a 'doubling'."""
    import invoices
    rid = _rid(db_path)
    _ingredient(db_path, rid, "Chicken Breast", unit="lb", cost=2.20, case_size=10)
    ln = invoices.propose(rid, {"lines": [_line("CHICKEN BREAST 4/10#", 2, "CS", 84.50)]})["lines"][0]
    assert ln["proposed_cost"] == 8.45 and ln["selected"] is False and "unit mix-up" in ln["note"]


def test_lines_summing_over_the_invoice_total_flag_a_misread(db_path):
    import invoices
    rid = _rid(db_path)
    p = invoices.propose(rid, {"invoice_total": 50.0, "lines": [_line("THING", 1, "EA", 80.0)]})
    assert p["total_check"]["plausible"] is False


def test_another_restaurants_ingredients_are_never_matched(db_path):
    import invoices
    mine, theirs = _rid(db_path), _rid(db_path, name="Other")
    _ingredient(db_path, theirs, "Salmon Fillet")
    ln = invoices.propose(mine, {"lines": [_line("SALMON FILLET", 10, "LB", 9.0)]})["lines"][0]
    assert ln["ingredient_id"] is None


# ── invoices: scan + apply ─────────────────────────────────────────────────

class _FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns JSON."""
    def __init__(self, payload, stop_reason="end_turn"):
        self.requests, self._payload, self._stop = [], payload, stop_reason
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        return SimpleNamespace(stop_reason=self._stop, usage=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(self._payload))])


@pytest.fixture
def no_budget(monkeypatch):
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)


def test_scan_sends_the_image_with_a_json_schema_and_stores_a_pending_import(db_path, no_budget):
    import invoices
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, "Olive Oil", unit="gal", cost=30.0)
    fake = _FakeClient({"supplier": "Sysco", "invoice_date": "2026-09-15", "invoice_total": 64.0,
                        "lines": [_line("OLIVE OIL EXTRA VIRGIN", 2, "GAL", 32.0)]})
    out = invoices.scan(rid, b"\x89PNG fake", "image/png", client=fake)
    req = fake.requests[0]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["messages"][0]["content"][0]["type"] == "image"
    assert out["id"] and out["lines"][0]["ingredient_id"] == iid
    assert _cost(db_path, iid) == 30.0, "scanning alone writes nothing"


def test_a_pdf_goes_as_a_document_block(db_path, no_budget):
    import invoices
    fake = _FakeClient({"supplier": None, "invoice_date": None, "invoice_total": None,
                        "lines": [_line("X", 1, "EA", 1.0)]})
    invoices.extract(_rid(db_path), b"%PDF-1.4", "application/pdf", client=fake)
    assert fake.requests[0]["messages"][0]["content"][0]["type"] == "document"


def test_the_same_file_twice_is_not_read_twice(db_path, no_budget):
    import invoices
    rid = _rid(db_path)
    fake = _FakeClient({"supplier": "S", "invoice_date": None, "invoice_total": None,
                        "lines": [_line("X", 1, "EA", 1.0)]})
    first = invoices.scan(rid, b"same bytes", "image/jpeg", client=fake)
    second = invoices.scan(rid, b"same bytes", "image/jpeg", client=fake)
    assert len(fake.requests) == 1 and second["duplicate"] is True and second["id"] == first["id"]


def test_unsupported_and_oversized_uploads_are_refused_before_any_call(db_path):
    import invoices
    with pytest.raises(invoices.InvoiceError):
        invoices.check_upload(b"x", "image/heic")
    with pytest.raises(invoices.InvoiceError):
        invoices.check_upload(b"x" * (invoices.MAX_IMAGE_BYTES + 1), "image/jpeg")
    with pytest.raises(invoices.InvoiceError):
        invoices.check_upload(b"", "image/jpeg")


def test_a_refusal_or_truncation_is_an_owner_facing_error(db_path, no_budget):
    import invoices
    for stop in ("refusal", "max_tokens"):
        fake = _FakeClient({"lines": []}, stop_reason=stop)
        with pytest.raises(invoices.InvoiceError):
            invoices.extract(_rid(db_path), b"img", "image/png", client=fake)


def test_apply_writes_only_confirmed_lines_and_records_old_costs(db_path, no_budget):
    import invoices
    rid = _rid(db_path)
    a = _ingredient(db_path, rid, "Ricotta", unit="lb", cost=3.0)
    b = _ingredient(db_path, rid, "Baby Spinach", unit="lb", cost=2.0)
    fake = _FakeClient({"supplier": "S", "invoice_date": None, "invoice_total": None,
                        "lines": [_line("RICOTTA", 5, "LB", 3.3), _line("BABY SPINACH", 4, "LB", 2.5)]})
    imp = invoices.scan(rid, b"inv", "image/png", client=fake)
    out = invoices.apply(rid, imp["id"], [{"index": 0, "ingredient_id": a, "unit_cost": 3.3}])
    assert out["ok"] and _cost(db_path, a) == 3.3 and _cost(db_path, b) == 2.0
    assert out["updated"][0]["old_cost"] == 3.0
    stored = invoices.get_import(rid, imp["id"])
    assert stored["applied"][0]["new_cost"] == 3.3 and stored["applied_at"]


def test_an_import_applies_once(db_path, no_budget):
    import invoices
    rid = _rid(db_path)
    a = _ingredient(db_path, rid, "Ricotta", unit="lb", cost=3.0)
    fake = _FakeClient({"supplier": "S", "invoice_date": None, "invoice_total": None,
                        "lines": [_line("RICOTTA", 5, "LB", 3.3)]})
    imp = invoices.scan(rid, b"inv", "image/png", client=fake)
    assert invoices.apply(rid, imp["id"], [{"ingredient_id": a, "unit_cost": 3.3}])["ok"]
    again = invoices.apply(rid, imp["id"], [{"ingredient_id": a, "unit_cost": 9.9}])
    assert again["ok"] is False and _cost(db_path, a) == 3.3


def test_apply_never_writes_another_restaurants_ingredient(db_path, no_budget):
    import invoices
    mine, theirs = _rid(db_path), _rid(db_path, name="Other")
    foreign = _ingredient(db_path, theirs, "Ricotta", cost=3.0)
    fake = _FakeClient({"supplier": "S", "invoice_date": None, "invoice_total": None,
                        "lines": [_line("RICOTTA", 5, "LB", 3.3)]})
    imp = invoices.scan(mine, b"inv", "image/png", client=fake)
    out = invoices.apply(mine, imp["id"], [{"ingredient_id": foreign, "unit_cost": 99.0}])
    assert out["updated"] == [] and _cost(db_path, foreign) == 3.0
    # And another restaurant cannot apply this restaurant's import at all.
    assert invoices.apply(theirs, imp["id"], [])["ok"] is False


# ── HTTP surface ───────────────────────────────────────────────────────────

@pytest.fixture
def app():
    from strategy_routes import strategy_bp, strategy_mobile_bp, issue_link_bp
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(strategy_bp)
    flask_app.register_blueprint(strategy_mobile_bp)
    flask_app.register_blueprint(issue_link_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _as(monkeypatch, rid, role="client"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.com"})


def test_comps_and_routing_are_owner_only_unless_granted(client, db_path, monkeypatch):
    rid = _rid(db_path)
    for role, want in (("manager", 403), ("member", 403), ("client", 200), ("owner", 200)):
        _as(monkeypatch, rid, role)
        for path in ("/api/loss-signals", "/api/issues/routing"):
            assert client.get(path).status_code == want, (role, path)


def test_a_manager_gets_their_own_brief_but_cannot_change_its_settings(client, db_path, monkeypatch):
    rid = _rid(db_path)
    _as(monkeypatch, rid, "manager")
    body = client.get("/api/morning-brief").get_json()
    assert body["ok"] and body["can_edit"] is False
    assert client.post("/api/morning-brief/settings", json={"hour": 6}).status_code == 403


def test_a_granted_manager_can_open_comps_and_voids(client, db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 5, "restaurant_id": rid, "is_admin": 0, "role": "manager",
        "grants": frozenset({"loss.view"}), "username": "gm", "email": "gm@x.com"})
    assert client.get("/api/loss-signals").status_code == 200

def test_a_manager_never_sees_food_cost_goals_or_invoices(client, db_path, monkeypatch):
    import goals
    rid = _rid(db_path, module_inventory=1)
    goals.set_goal(rid, "food_cost_pct", 28)
    goals.set_goal(rid, "labor_pct", 30)
    _as(monkeypatch, rid, "manager")
    metrics_seen = {g["metric"] for g in client.get("/api/goals").get_json()["goals"]}
    assert metrics_seen == {"labor_pct"}
    assert client.post("/api/goals", json={"metric": "food_cost_pct", "target": 25}).status_code == 403
    assert client.get("/api/food-cost/invoices").status_code == 403


def test_every_web_route_has_a_mobile_twin(app):
    web = {str(r) for r in app.url_map.iter_rules() if str(r).startswith("/api/")}
    mobile = {str(r) for r in app.url_map.iter_rules() if str(r).startswith("/mobile/api/")}
    assert {w[len("/api"):] for w in web} == {m[len("/mobile/api"):] for m in mobile}


def test_the_mobile_twin_answers_with_a_bearer_token(client, db_path):
    auth.init_auth(db_path=db_path)
    rid = _rid(db_path)
    uid = auth.create_user(rid, "own", "own@x.com", "pw", db_path=db_path)
    token = auth.create_session(uid, db_path=db_path)
    resp = client.get("/mobile/api/issues", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert client.get("/mobile/api/issues").status_code == 401


def test_morning_brief_hour_is_bounded(client, db_path, monkeypatch):
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    assert client.post("/api/morning-brief/settings", json={"hour": 23}).status_code == 400
    assert client.post("/api/morning-brief/settings", json={"hour": 6, "enabled": False}).status_code == 200
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.morning_brief_hour == 6 and r.morning_brief_enabled == 0


def test_outcome_window_is_bounded(client, db_path, monkeypatch):
    _as(monkeypatch, _rid(db_path))
    resp = client.post("/api/outcomes", json={"title": "x", "metric": "sales", "window_days": 5000})
    assert resp.status_code == 400


def test_invoice_upload_route_rejects_a_missing_file(client, db_path, monkeypatch):
    _as(monkeypatch, _rid(db_path, module_inventory=1))
    assert client.post("/api/food-cost/invoices").status_code == 400


# ── the public issue link ──────────────────────────────────────────────────

def _issue(db_path, monkeypatch):
    import issues
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)
    rid = _rid(db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?,?,?,1)", (rid, "Maria", "+15555550100")).lastrowid
    conn.commit(); conn.close()
    issue, token = issues.create_issue(rid, "manual", "Walk-in is warm", assignee_contact_id=cid)
    return issue, token


def test_opening_the_link_does_not_acknowledge(client, db_path, monkeypatch):
    """Messaging apps fetch links to build previews. If a GET acknowledged,
    every issue would be acknowledged before a person saw it, and the
    escalation it exists for would never fire."""
    import issues
    issue, token = _issue(db_path, monkeypatch)
    for _ in range(3):
        assert client.get(f"/i/{token}").status_code == 200
    assert issues.by_token(token)["status"] == "open"


def test_the_buttons_acknowledge_then_resolve(client, db_path, monkeypatch):
    import issues
    issue, token = _issue(db_path, monkeypatch)
    assert client.post(f"/i/{token}", data={"action": "ack"}).status_code == 200
    assert issues.by_token(token)["status"] == "acknowledged"
    body = client.post(f"/i/{token}", data={"action": "resolve", "note": "reset breaker"})
    assert b"Marked resolved" in body.data
    assert issues.by_token(token)["resolution_note"] == "reset breaker"


def test_a_bad_token_or_action_is_refused(client, db_path, monkeypatch):
    _issue(db_path, monkeypatch)
    assert client.get("/i/" + "x" * 32).status_code == 404
    _, token = _issue(db_path, monkeypatch)
    assert client.post(f"/i/{token}", data={"action": "delete"}).status_code == 400


# ── scheduled jobs ─────────────────────────────────────────────────────────

def test_auto_draft_skips_opt_outs_external_tools_and_recent_schedules(db_path, monkeypatch):
    import strategy_jobs, ops
    ran = []
    monkeypatch.setattr("schedule_engine._run_schedule_job", lambda job_id, rid: ran.append(rid))
    monkeypatch.setattr(ops, "start_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "done"})
    monkeypatch.setattr("push.fire_push", lambda *a, **k: None)
    on = _rid(db_path, name="On", module_labor=1)
    off = _rid(db_path, name="Off", module_labor=1)
    ext = _rid(db_path, name="Ext", module_labor=1)
    recent = _rid(db_path, name="Recent", module_labor=1)
    models.update_restaurant(on, {"auto_draft_schedule": 1}, db_path=db_path)
    models.update_restaurant(ext, {"auto_draft_schedule": 1, "external_scheduling_tool": "7shifts"},
                             db_path=db_path)
    models.update_restaurant(recent, {"auto_draft_schedule": 1}, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at) VALUES (?, datetime('now','-1 day'))",
                 (recent,))
    conn.commit(); conn.close()
    out = strategy_jobs.run_auto_draft_schedules(db_path=db_path)
    assert ran == [on] and out["drafted"] == 1


def test_auto_draft_does_not_announce_a_draft_that_failed(db_path, monkeypatch):
    import strategy_jobs, ops
    pushed = []
    monkeypatch.setattr("schedule_engine._run_schedule_job", lambda job_id, rid: None)
    monkeypatch.setattr(ops, "start_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "error"})
    monkeypatch.setattr("push.fire_push", lambda *a, **k: pushed.append(a))
    rid = _rid(db_path, module_labor=1)
    models.update_restaurant(rid, {"auto_draft_schedule": 1}, db_path=db_path)
    assert strategy_jobs.run_auto_draft_schedules(db_path=db_path)["drafted"] == 0 and not pushed


def test_loss_sync_treats_an_unsupported_pos_as_normal(db_path, monkeypatch):
    import strategy_jobs, pos
    _rid(db_path)
    def _unsupported(*a, **k):
        raise pos.POSCapabilityError("no comps here")
    monkeypatch.setattr(pos, "fetch_loss_lines", _unsupported)
    assert strategy_jobs.run_loss_sync(db_path=db_path) == {"synced": 0, "not_supported": 1}


# ── campaign outcome tracking ──────────────────────────────────────────────

def test_a_slow_day_campaign_is_tracked_only_once_it_actually_sent(db_path):
    import client_api, outcomes
    rid = _rid(db_path)
    client_api._track_campaign_outcome(rid, {"target_day": "tuesday"}, {"ok": False}, None)
    assert outcomes.list_outcomes(rid) == []
    client_api._track_campaign_outcome(rid, {"target_day": "tuesday"}, {"ok": True}, None)
    rows = outcomes.list_outcomes(rid)
    assert len(rows) == 1 and rows[0]["metric"] == "weekday_sales:Tuesday"
    client_api._track_campaign_outcome(rid, {"target_day": "Funday"}, {"ok": True}, None)
    assert len(outcomes.list_outcomes(rid)) == 1


# ── staff pre-shift ────────────────────────────────────────────────────────

def test_the_preshift_briefing_carries_no_money(db_path, monkeypatch):
    import preshift, labor
    rid = _rid(db_path)
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {"days": [
        {"day": "Friday", "median_sales": 9123.45, "samples": 6, "vs_average_pct": 32}]})
    from datetime import date
    out = preshift.build(rid, day=date(2026, 9, 18))
    text = " ".join(i["text"] for i in out["items"])
    assert "busy Friday" in text and "32%" in text
    assert "$" not in text and "9123" not in text and "9,123" not in text


# ── weekly executive review ────────────────────────────────────────────────

def test_the_digest_adds_goals_issues_and_nothing_when_there_is_nothing(db_path, monkeypatch):
    import reporter, goals, issues
    rid = _rid(db_path)
    assert reporter._follow_through_sections(rid) == []
    monkeypatch.setattr(goals, "progress", lambda *a, **k: [
        {"state": "moving_right_way", "label": "Labor %", "metric": "labor_pct"}])
    monkeypatch.setattr(goals, "summarise", lambda g: "Labor % 29.1% against a 28% target")
    monkeypatch.setattr(issues, "summary", lambda *a, **k: {
        "open": 1, "acknowledged": 2, "resolved_last_7_days": 3, "oldest_open_hours": 5})
    html = "".join(reporter._follow_through_sections(rid))
    assert "Labor % 29.1%" in html and "3 resolved this week" in html
    assert "not yet acknowledged" in html


def test_a_failing_block_drops_only_that_block(db_path, monkeypatch):
    import reporter, goals, issues
    rid = _rid(db_path)
    monkeypatch.setattr(goals, "progress", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(issues, "summary", lambda *a, **k: {
        "open": 0, "acknowledged": 1, "resolved_last_7_days": 0})
    html = "".join(reporter._follow_through_sections(rid))
    assert "1 in hand" in html


def test_the_loss_block_needs_an_owner_view(db_path, monkeypatch):
    """The preview digest goes to whoever is signed in — managers included —
    and a loss signal can name the approving manager."""
    import reporter, loss_detection
    rid = _rid(db_path)
    monkeypatch.setattr(loss_detection, "signals", lambda *a, **k: {
        "flagged": [{"headline": "One manager (POS id 7) approved 80% of comps", "alternative": "x"}],
        "note": "not a finding of wrongdoing"})
    assert "POS id 7" not in "".join(reporter._follow_through_sections(rid))
    assert "POS id 7" in "".join(reporter._follow_through_sections(rid, owner_view=True))


def test_the_preshift_briefing_never_quotes_a_guest(db_path, monkeypatch):
    """A complaint as written can name an employee."""
    import preshift, review_intelligence, labor
    rid = _rid(db_path, module_reviews=1)
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {"days": []})
    monkeypatch.setattr(review_intelligence, "complaint_clusters", lambda *a, **k: [
        {"category": "service_speed", "mentions": 5, "weekday": {"value": "Friday"},
         "complaints": [{"complaint": "our server Jake ignored us for 20 minutes"}]}])
    from datetime import date
    text = " ".join(i["text"] for i in preshift.build(rid, day=date(2026, 9, 18))["items"])
    assert "service speed" in text and "Jake" not in text


# ── owner-granted access, end to end through real sessions ─────────────────

@pytest.fixture
def team_app(db_path, monkeypatch):
    import mobile_api, client_api, push
    from strategy_routes import strategy_mobile_bp
    auth.init_auth(db_path=db_path)
    for m in (mobile_api, client_api):
        monkeypatch.setattr(m, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _login(db_path, rid, name, role):
    uid = auth.create_user(rid, name, f"{name}@x.com", "pw", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit(); conn.close()
    return uid, {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}


def test_owner_grants_food_cost_to_the_gm_and_it_applies_on_the_next_request(team_app, db_path):
    rid = _rid(db_path, module_inventory=1, module_labor=1)
    owner, oh = _login(db_path, rid, "erik", "client")
    gm, gh = _login(db_path, rid, "gm", "manager")
    path = "/mobile/api/food-cost/dish-scorecard"
    assert team_app.get(path, headers=gh).status_code == 403
    r = team_app.post(f"/mobile/api/account/team/{gm}/access", headers=oh,
                      json={"permission": "foodcost.view", "enabled": True})
    assert r.status_code == 200 and r.get_json()["access"] == ["foodcost.view"]
    assert team_app.get(path, headers=gh).status_code == 200
    assert team_app.get("/mobile/api/loss-signals", headers=gh).status_code == 403, \
        "comps & voids are a separate grant"
    team_app.post(f"/mobile/api/account/team/{gm}/access", headers=oh,
                  json={"permission": "foodcost.view", "enabled": False})
    assert team_app.get(path, headers=gh).status_code == 403


def test_only_the_owner_changes_access(team_app, db_path):
    rid = _rid(db_path, module_inventory=1)
    owner, oh = _login(db_path, rid, "erik", "client")
    gm, gh = _login(db_path, rid, "gm", "manager")
    agm, _ = _login(db_path, rid, "agm", "manager")
    for target in (gm, agm):
        r = team_app.post(f"/mobile/api/account/team/{target}/access", headers=gh,
                          json={"permission": "foodcost.view", "enabled": True})
        assert r.status_code == 403
    other = _rid(db_path, name="Other")
    stranger, _ = _login(db_path, other, "stranger", "manager")
    r = team_app.post(f"/mobile/api/account/team/{stranger}/access", headers=oh,
                      json={"permission": "foodcost.view", "enabled": True})
    assert r.status_code == 400
    r = team_app.post(f"/mobile/api/account/team/{gm}/access", headers=oh,
                      json={"permission": "team.invite", "enabled": True})
    assert r.status_code == 400


def test_the_team_list_shows_each_managers_access_and_brief(team_app, db_path):
    rid = _rid(db_path, module_inventory=1)
    owner, oh = _login(db_path, rid, "erik", "client")
    gm, _ = _login(db_path, rid, "gm", "manager")
    team_app.post(f"/mobile/api/account/team/{gm}/access", headers=oh, json={"morning_brief": False})
    body = team_app.get("/mobile/api/account/team", headers=oh).get_json()
    row = next(m for m in body["members"] if m["id"] == gm)
    assert row["access_grantable"] and row["access"] == [] and row["morning_brief"] is False
    assert {o["key"] for o in body["access_options"]} == {"foodcost.view", "loss.view"}
    assert body["can_edit_access"] is True


# ── co-owners and roles ────────────────────────────────────────────────────

def test_an_owner_invites_a_co_owner_who_can_do_everything(team_app, db_path, monkeypatch):
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **k: None)
    rid = _rid(db_path, module_inventory=1)
    erik, eh = _login(db_path, rid, "erik", "client")
    r = team_app.post("/mobile/api/account/team/invite", headers=eh,
                      json={"name": "Jim", "email": "jim@x.com", "role": "client"})
    assert r.status_code == 200 and r.get_json()["role"] == "client"
    jim = r.get_json()["user_id"]
    jh = {"Authorization": f"Bearer {auth.create_session(jim, db_path=db_path)}"}
    assert team_app.get("/mobile/api/loss-signals", headers=jh).status_code == 200
    assert team_app.get("/mobile/api/food-cost/dish-scorecard", headers=jh).status_code == 200
    body = team_app.get("/mobile/api/account/team", headers=jh).get_json()
    assert body["can_edit_access"] is True, "a co-owner manages the team too"


def test_a_role_change_applies_on_the_next_request(team_app, db_path):
    rid = _rid(db_path, module_inventory=1)
    erik, eh = _login(db_path, rid, "erik", "client")
    gm, gh = _login(db_path, rid, "gm", "member")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO memberships (user_id, restaurant_id, role) VALUES (?,?, 'member')", (gm, rid))
    conn.commit(); conn.close()
    r = team_app.post(f"/mobile/api/account/team/{gm}/role", headers=eh, json={"role": "manager"})
    assert r.status_code == 200 and r.get_json()["role_label"] == "Manager"
    # A member reads food cost (legacy); a manager doesn't until granted — so
    # this 403 proves the new role, including the membership row, took effect.
    assert team_app.get("/mobile/api/food-cost/dish-scorecard", headers=gh).status_code == 403


def test_the_last_owner_can_never_be_demoted_or_removed(team_app, db_path):
    rid = _rid(db_path)
    erik, eh = _login(db_path, rid, "erik", "client")
    jim, jh = _login(db_path, rid, "jim", "client")
    # Two owners: Erik may demote Jim...
    assert team_app.post(f"/mobile/api/account/team/{jim}/role", headers=eh,
                         json={"role": "manager"}).status_code == 200
    # ...but Jim, now a manager, can't change anything, and nobody changes
    # their own role.
    assert team_app.post(f"/mobile/api/account/team/{erik}/role", headers=jh,
                         json={"role": "member"}).status_code == 403
    assert team_app.post(f"/mobile/api/account/team/{erik}/role", headers=eh,
                         json={"role": "member"}).status_code == 400
    # Directly at the auth layer: demoting or removing the only owner fails.
    with pytest.raises(auth.TeamAccessError):
        auth.set_team_role(rid, erik, "manager", acting_user_id=jim, db_path=db_path)
    assert auth.revoke_team_member(rid, erik, jim, db_path=db_path)["ok"] is False


def test_roles_are_a_fixed_list(team_app, db_path):
    rid = _rid(db_path)
    erik, eh = _login(db_path, rid, "erik", "client")
    gm, _ = _login(db_path, rid, "gm", "manager")
    for bad in ("owner", "admin", "employee", ""):
        assert team_app.post(f"/mobile/api/account/team/{gm}/role", headers=eh,
                             json={"role": bad}).status_code == 400
    assert team_app.post("/mobile/api/account/team/invite", headers=eh,
                         json={"name": "X", "email": "x@x.com", "role": "owner"}).status_code == 400


def test_the_weekly_digest_goes_to_every_owner(db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: sqlite_conn(db_path))
    auth.init_auth(db_path=db_path)
    rid = _rid(db_path)
    for name, role in (("erik", "client"), ("jim", "client"), ("gm", "manager")):
        _login(db_path, rid, name, role)
    assert scheduler.get_owner_emails(rid) == ["erik@x.com", "jim@x.com"]
    assert scheduler.get_owner_email(rid) == "erik@x.com"


def sqlite_conn(path):
    import sqlite3
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c
