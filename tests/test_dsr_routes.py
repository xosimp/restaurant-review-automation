"""The DSR's HTTP surface and who sees what (dsr.access, strategy_routes).

Will's rule: every OWNER login at a restaurant gets the Owner DSR, every
MANAGER login the Manager DSR. One function decides it (access.view_for),
both views render from the same stored facts, and the manager view leaves
out what permissions.py already reserves: loss lines (LOSS_VIEW), food cost
(FOOD_COST_VIEW), and the owner-only financials — budget, prime cost.
"""
import sys
import types
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import pos
import dsr
from dsr import access, pipeline, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)


@pytest.fixture
def db(db_path, monkeypatch):
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
    return db_path


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client", **extra):
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 7, "restaurant_id": rid, "is_admin": 0, "role": role, "username": "u", "email": "u@x.com", **extra})


def _rid(db, name="View Co"):
    return create_restaurant(Restaurant(name=name, owner_email="v@x.com"), db_path=db)


SALES = dsr.block(dsr.READY, source="rpower", metrics={
    "net": 2000, "gross": 2100, "comps": 40, "voids": 25, "refunds": 0, "discounts": 60,
    "budget_net": 1900, "vs_budget_net": 100, "vs_budget_net_pct": 5.3, "vs_yesterday_pct": 12.5},
    detail={"budget": {"gross": 2000, "net": 1900}, "source_checks": {"ticket_type_comp": -40},
            "hourly": [{"hour": "19", "net": 1200}]})
FOOD = dsr.block(dsr.READY, source="cavnar", metrics={"cost_pct": 29.0, "prime_cost_pct": 58.0})
LABOR = dsr.block(dsr.READY, source="rpower", metrics={"pct": 22.0, "cost": 440})
NARRATIVE = {
    "executive_summary": {"text": "Net $2,000, $100 over budget.", "facts": ["sales.net", "sales.vs_budget_net"]},
    "went_well": [{"text": "Up 12.5% on Monday.", "facts": ["sales.vs_yesterday_pct"]},
                  {"text": "Food cost held at 29%.", "facts": ["food.cost_pct"]}],
    "needs_attention": ["A sentence that cites nothing."],
    "meta": {"model": "fake"},
}


def _facts():
    return {"schema": 1, "blocks": {"sales": SALES, "labor": LABOR, "food": FOOD}, "missing": []}


def _report(db, rid, day=DAY):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    for name, blk in (("sales", SALES), ("labor", LABOR), ("food", FOOD)):
        store.save_block(r["id"], name, blk, db_path=db)
    store.save_narrative(r["id"], NARRATIVE, db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return r


# ── the one decision ────────────────────────────────────────────────────────

@pytest.mark.parametrize("user, view", [
    ({"role": "owner"}, access.OWNER),
    ({"role": "client"}, access.OWNER),              # the restaurant's primary login is its owner
    ({"role": "manager", "is_admin": 1}, access.OWNER),   # Cavnar admin
    ({"role": "manager"}, access.MANAGER),
    ({"role": "member"}, access.MANAGER),
    ({"role": "employee"}, None),                     # a PIN identity has no console
    ({"role": "support"}, None),
    (None, None),
])
def test_view_for_is_the_owner_or_manager_dsr_by_role(user, view):
    assert access.view_for(user) == view


def test_the_owner_view_is_everything():
    facts, hidden = access.redact(_facts(), {"role": "client"})
    assert facts["blocks"]["sales"]["metrics"] == SALES["metrics"]
    assert facts["blocks"]["sales"]["detail"]["budget"] and set(facts["blocks"]) == {"sales", "labor", "food"}
    assert hidden == set() and facts["withheld"] == []
    assert access.filter_narrative(NARRATIVE, hidden) == NARRATIVE


def test_the_manager_view_leaves_out_loss_food_cost_and_owner_financials():
    facts, hidden = access.redact(_facts(), {"role": "manager"})
    m = facts["blocks"]["sales"]["metrics"]
    for gone in ("comps", "voids", "refunds", "budget_net", "vs_budget_net", "vs_budget_net_pct"):
        assert gone not in m, gone
    assert (m["net"], m["discounts"], m["vs_yesterday_pct"]) == (2000.0, 60.0, 12.5)
    # Gross beside net and discounts IS the comps (D2-2): it goes with them.
    assert "gross" not in m and "sales.gross" in hidden
    assert set(facts["blocks"]["sales"]["detail"]) == {"hourly"}
    assert "food" not in facts["blocks"] and facts["withheld"] == ["food"]
    assert facts["blocks"]["labor"]["metrics"]["pct"] == 22.0
    n = access.filter_narrative(NARRATIVE, hidden)
    # What cites a withheld fact goes, and so does what cites nothing.
    assert n == {"went_well": [{"text": "Up 12.5% on Monday.", "facts": ["sales.vs_yesterday_pct"]}],
                 "needs_attention": [], "meta": {"model": "fake"}}


def test_an_owner_grant_opens_comps_or_food_cost_to_a_manager_but_never_the_budget():
    loss, _ = access.redact(_facts(), {"role": "manager", "grants": frozenset({"loss.view"})})
    assert loss["blocks"]["sales"]["metrics"]["comps"] == 40.0
    assert loss["blocks"]["sales"]["metrics"]["gross"] == 2100.0      # granted comps, so gross derives nothing new
    assert "budget_net" not in loss["blocks"]["sales"]["metrics"] and "food" not in loss["blocks"]
    food, _ = access.redact(_facts(), {"role": "manager", "grants": frozenset({"foodcost.view"})})
    assert food["blocks"]["food"]["metrics"] == {"cost_pct": 29.0}       # prime cost stays the owner's


# ── the routes ──────────────────────────────────────────────────────────────

def test_an_owner_reads_the_owner_dsr_and_a_manager_the_manager_dsr(client, db, monkeypatch):
    rid = _rid(db)
    _report(db, rid)
    _as(monkeypatch, rid, "client")
    body = client.get("/api/dsr/2026-09-22").get_json()
    assert body["ok"] and body["view"] == "owner" and body["label"] == "9/22/26"
    assert body["facts"]["blocks"]["sales"]["metrics"]["budget_net"] == 1900.0
    assert body["narrative"] == NARRATIVE and body["status"] == "final"
    assert [v["version"] for v in body["versions"]] == [1]
    _as(monkeypatch, rid, "manager")
    body = client.get("/api/dsr/2026-09-22").get_json()
    assert body["view"] == "manager" and "budget_net" not in body["facts"]["blocks"]["sales"]["metrics"]
    assert body["facts"]["withheld"] == ["food"] and "executive_summary" not in body["narrative"]
    assert [b["name"] for b in body["checklist"]["blocks"]] == ["sales", "labor", "reviews", "marketing",
                                                                "intel", "closeout"]


@pytest.mark.parametrize("role", ["employee", "support"])
def test_a_login_with_no_console_is_refused_every_dsr_route(client, db, monkeypatch, role):
    rid = _rid(db)
    _report(db, rid)
    _as(monkeypatch, rid, role)
    for method, path in (("get", "/api/dsr"), ("get", "/api/dsr/2026-09-22"),
                         ("get", "/api/dsr/2026-09-22/status"), ("post", "/api/dsr/close")):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, (role, path)
        assert "facts" not in (resp.get_json() or {})


def test_a_manager_only_ever_reads_their_own_location(client, db, monkeypatch):
    mine, theirs = _rid(db, "Lakeview"), _rid(db, "Wicker Park")
    _report(db, theirs)
    _as(monkeypatch, mine, "manager")
    assert client.get("/api/dsr/2026-09-22").status_code == 404
    assert client.get("/api/dsr").get_json()["reports"] == []
    assert client.get("/api/dsr/2026-09-22/status").get_json()["exists"] is False


def test_the_list_and_the_status_checklist(client, db, monkeypatch):
    rid = _rid(db)
    _report(db, rid)
    _as(monkeypatch, rid, "manager")
    rows = client.get("/api/dsr").get_json()
    assert rows["view"] == "manager"
    # The lead cites the budget, withheld from a manager, so it goes; the
    # summary never falls back to an unfiltered sentence — and it says why
    # there is none (D2-4: it used to say nothing).
    assert rows["reports"] == [{"business_date": "2026-09-22", "label": "9/22/26", "version": 1, "status": "final",
                                "provisional": False, "missing": [], "finalized_at": rows["reports"][0]["finalized_at"],
                                "net": 2000.0, "lead": None, "lead_missing": access.NO_LEAD_FOR_VIEW}]
    assert rows["enabled"] is True and len(rows["tonight"]) == 10
    st = client.get("/api/dsr/2026-09-22/status").get_json()
    assert st["exists"] is True and st["status"] == "final"
    keys = [s["key"] for s in st["stages"]]
    assert keys == ["scheduled", "awaiting_close", "collecting", "writing", "final"]
    assert all(s["done"] for s in st["stages"] if s["at"])
    assert client.get("/api/dsr/2026-13-40").status_code == 400
    assert client.get("/api/dsr/tonight/status").status_code == 400


def test_the_mobile_twin_answers_with_a_bearer_token(client, db):
    auth.init_auth(db_path=db)
    rid = _rid(db)
    _report(db, rid)
    uid = auth.create_user(rid, "own", "own@x.com", "pw", db_path=db)
    token = auth.create_session(uid, db_path=db)
    resp = client.get("/mobile/api/dsr/2026-09-22", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.get_json()["view"] == "owner"
    assert client.get("/mobile/api/dsr/2026-09-22").status_code == 401


# ── Close day ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake_night(monkeypatch):
    monkeypatch.setattr(pipeline, "_spawn", lambda fn: fn())
    # These tests press Close day at whatever the wall clock says; the
    # before-close refusal (a POS with no close-day record) is tested on
    # its own below and in test_dsr_pipeline with a fixed clock.
    monkeypatch.setattr(pipeline, "manual_close_refusal", lambda r, d, now_utc=None: None)
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("fakepos", object()))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: ({
        "gross": 1000.0, "net": 950.0, "transactions": 40, "guests": None, "discounts": 50.0, "comps": 0.0,
        "voids": 0.0, "refunds": 0.0, "tax": 80.0, "by_department": {}, "by_hour": {}, "items": [],
        "net_deductions": ["discounts", "comps"], "source_checks": {}}, "fakepos"))
    for name in ("food", "reviews", "marketing", "intel", "closeout"):
        mod = types.ModuleType(f"dsr.block_{name}")
        mod.collect = lambda ctx: dsr.block(dsr.READY, source="fake")
        monkeypatch.setitem(sys.modules, f"dsr.block_{name}", mod)
    narrative = types.ModuleType("dsr.narrative")
    narrative.write = lambda ctx, facts: {"ok": False, "narrative": None, "reason": "test"}
    monkeypatch.setitem(sys.modules, "dsr.narrative", narrative)


def test_close_day_starts_tonight_now(client, db, monkeypatch, fake_night):
    import closeout
    rid = _rid(db)
    _as(monkeypatch, rid, "manager")
    resp = client.post("/api/dsr/close", json={})
    body = resp.get_json()
    assert resp.status_code == 202 and body["started"] is True
    today = closeout.business_date_for(get_restaurant(rid, db_path=db))
    rep = store.get_report(rid, today, db_path=db)
    assert rep["trigger"] == "manual" and rep["stages"]["closed_by"] == "manual"
    assert rep["facts"]["blocks"]["sales"]["metrics"]["net"] == 950.0


def test_close_day_before_close_is_refused_unless_the_owner_says_they_closed_early(client, db, monkeypatch,
                                                                                    fake_night):
    # D1-1: a POS with no close-day record takes the button's word, so
    # before close the button is refused rather than finalising half a night.
    import closeout
    rid = _rid(db)
    monkeypatch.setattr(pipeline, "manual_close_refusal",
                        lambda r, d, now_utc=None: "It's before your close (11:00 pm).")
    _as(monkeypatch, rid, "manager")
    resp = client.post("/api/dsr/close", json={"early": True})
    body = resp.get_json()
    assert resp.status_code == 409 and body["code"] == "before_close" and body["needs_confirm"] is False
    today = closeout.business_date_for(get_restaurant(rid, db_path=db))
    assert store.get_report(rid, today, db_path=db) is None
    _as(monkeypatch, rid, "client")
    resp = client.post("/api/dsr/close", json={})
    assert resp.status_code == 409 and resp.get_json()["needs_confirm"] is True
    assert client.post("/api/dsr/close", json={"early": True}).status_code == 202
    rep = store.get_report(rid, today, db_path=db)
    assert rep["stages"]["closed_by"] == "manual" and rep["facts"]["blocks"]["sales"]["metrics"]["net"] == 950.0


def test_only_the_owner_can_rerun_a_finished_night(client, db, monkeypatch, fake_night):
    import closeout
    rid = _rid(db)
    today = closeout.business_date_for(get_restaurant(rid, db_path=db))
    _report(db, rid, day=today)
    _as(monkeypatch, rid, "manager")
    assert client.post("/api/dsr/close", json={"rerun": True}).status_code == 403
    already = client.post("/api/dsr/close", json={})
    assert already.status_code == 200 and already.get_json()["started"] is False
    _as(monkeypatch, rid, "client")
    assert client.post("/api/dsr/close", json={"rerun": True}).status_code == 202
    assert [v["version"] for v in store.versions(rid, today, db_path=db)] == [1, 2]
    assert store.get_report(rid, today, db_path=db)["trigger"] == "manual"


def test_close_day_only_for_tonight_or_the_night_before(client, db, monkeypatch, fake_night):
    """A manager closes tonight or last night; the owner's seven-night window
    is tested in test_dsr_views."""
    import closeout
    rid = _rid(db)
    _as(monkeypatch, rid, "manager")
    today = closeout.business_date_for(get_restaurant(rid, db_path=db))
    old = (today - timedelta(days=3)).isoformat()
    assert client.post("/api/dsr/close", json={"date": old}).status_code == 400
    assert client.post("/api/dsr/close", json={"date": (today - timedelta(days=1)).isoformat()}).status_code == 202
    _as(monkeypatch, rid, "client")
    update_restaurant(rid, {"dsr_enabled": 0}, db_path=db)
    assert client.post("/api/dsr/close", json={}).status_code == 409


def test_the_manager_view_keeps_narrative_items_by_their_cites():
    """dsr.narrative writes items as {"text", "cites"}; the manager filter read
    only "facts", so every item looked uncited and a manager (who always has
    budget withheld) saw an empty summary. Items citing visible facts stay;
    items citing a withheld one go."""
    from dsr import access
    narrative = {
        "executive_summary": {"text": "Net sales were $4,212, up 15.3% on last week.", "cites": ["sales.net", "sales.net_vs_last_week_pct"]},
        "went_well": [{"text": "Labor held at 24.1%.", "cites": ["labor.pct"]},
                      {"text": "Beat budget by $712.", "cites": ["sales.vs_budget_gross"]}],
        "actions_tomorrow": [{"text": "Trim one server after 8pm.", "why": "Hours after 8pm ran ahead of sales.",
                              "cites": ["labor.hours_after_6pm"], "urgency": "today"}],
    }
    out = access.filter_narrative(narrative, {"sales.vs_budget_gross"})
    assert out["executive_summary"]["text"].startswith("Net sales were $4,212")
    assert [i["text"] for i in out["went_well"]] == ["Labor held at 24.1%."]
    assert out["actions_tomorrow"][0]["why"] == "Hours after 8pm ran ahead of sales."
