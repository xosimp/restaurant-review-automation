"""The in-person sales audit: storage, autosave versioning, the opportunity
engine's honesty rules (ranges, no double counting, insufficient data never
becomes a number), and the routes that keep internal notes off the
customer-facing report."""
import json

import pytest

import auth
import models
import sales_audits as store
import sales_audit_engine as engine
import sales_audit_routes
from sales_audit_schema import QUESTIONS, completion, is_visible


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, store):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(store, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    store.init_sales_audits(db_path=db_path)


FULL = {
    "restaurant_name": "Test Tavern", "owner_name": "Erik", "restaurant_type": "Upscale sports bar", "service_model": "Full-service",
    "fin_annual_revenue": "2400000", "fin_avg_check": "38",
    "lab_labor_pct": "34", "lab_overtime": "yes", "lab_ot_dollars_week": "900",
    "food_cost_pct": "36", "food_waste_week": "600",
    "bar_alcohol_pct": "45", "bar_bev_cost_pct": "26", "bar_variance_pct": "8",
    "rev_google_rating": "4.2", "rev_response_rate": "30",
    "mkt_spend_month": "2500", "mkt_roi_tracked": "no",
    "wl_walkaways_week": "8",
    "ops_comps_month": "3000", "ops_comps_monitored": "No",
    "tech_tools": [{"category": "Scheduling", "name": "7shifts", "cost": "150", "replace": "yes"},
                   {"category": "POS", "name": "Toast", "cost": "300", "replace": "yes"}],
}


# ── engine ───────────────────────────────────────────────────────────────────

def test_empty_audit_produces_no_numbers_and_no_score():
    r = engine.compute({})
    assert r["totals"]["annual"] == {"low": 0, "likely": 0, "high": 0}
    assert r["health"]["score"] is None
    assert all(c["status"] == "insufficient" for c in r["categories"].values())
    assert r["problems"] == [] and r["opportunities"] == []
    assert r["missing"]  # it tells Will what to ask


def test_ranges_never_a_single_number():
    r = engine.compute(FULL)
    for c in r["categories"].values():
        if c["status"] == "ok":
            assert c["low"] < c["high"] and c["low"] <= c["likely"] <= c["high"]
    assert r["totals"]["annual"]["low"] < r["totals"]["annual"]["high"]


def test_labor_takes_larger_of_gap_and_overtime_never_sum():
    r = engine.compute(FULL)
    lab = r["categories"]["labor"]
    # gap: 2,400,000 × (34 − 32)% = 48,000 → likely 24,000. OT: 900×52/3 = 15,600 → likely 10,920.
    assert lab["likely"] == 24000
    assert "LARGER" in lab["calc"]["overlap"]
    # overtime alone (no labor %) still sizes, but at low confidence
    r2 = engine.compute({"fin_annual_revenue": "2400000", "lab_overtime": "yes", "lab_ot_dollars_week": "900"})
    assert r2["categories"]["labor"]["status"] == "ok" and r2["categories"]["labor"]["confidence"] == "low"
    assert r2["categories"]["labor"]["likely"] == 10900


def test_food_waste_not_added_on_top_of_gap():
    r = engine.compute(FULL)
    food = r["categories"]["food"]
    # food sales = 2.4M × 55% = 1.32M; gap 36−33 = 3 pts → 39,600 → likely 19,800; waste 31,200 → likely 12,500. Max wins.
    assert food["likely"] == 19800
    assert "never both" in food["calc"]["overlap"]


def test_bar_variance_inside_pour_cost():
    r = engine.compute(FULL)
    bar = r["categories"]["bar"]
    assert bar["status"] == "ok" and bar["module"]["live"] is False
    assert "never both" in bar["calc"]["overlap"]


def test_combined_food_cost_does_not_double_count_bar():
    a = dict(FULL, food_includes_bev="Combined")
    r = engine.compute(a)
    assert r["categories"]["bar"]["status"] == "insufficient"
    assert "Food Cost" in r["categories"]["bar"]["opportunity"]


def test_performing_well_is_reported_honestly():
    a = {"fin_annual_revenue": "2000000", "lab_labor_pct": "29", "lab_target_pct": "30", "lab_know_daily": "yes", "lab_schedule_how": "Based on forecasted sales",
         "rev_google_rating": "4.8", "rev_response_rate": "95", "rev_response_time": "Same day"}
    r = engine.compute(a)
    assert r["categories"]["labor"]["status"] == "none" and r["categories"]["labor"]["likely"] == 0
    assert r["categories"]["reviews"]["status"] == "none"
    assert r["scores"]["labor"]["score"] == 100
    assert any("Labor" in w for w in r["wins"])


def test_owner_target_beats_benchmark():
    a = {"fin_annual_revenue": "1000000", "lab_labor_pct": "33", "lab_target_pct": "28"}
    r = engine.compute(a)
    assert r["categories"]["labor"]["calc"]["gap_pts"] == 5.0
    assert "owner's own" in " ".join(r["categories"]["labor"]["calc"]["assumptions"])


def test_invalid_values_are_ignored_not_used():
    a = {"fin_annual_revenue": "-5", "lab_labor_pct": "140", "rev_google_rating": "9", "food_cost_pct": "abc"}
    r = engine.compute(a)
    assert r["financials"].get("annual_revenue") is None
    assert r["categories"]["labor"]["status"] == "insufficient"
    assert r["categories"]["reviews"]["status"] == "insufficient"
    z = engine.compute({"fin_annual_revenue": "0", "lab_labor_pct": "0"})
    assert z["categories"]["labor"]["status"] == "insufficient"


def test_unusually_high_values_do_not_break():
    r = engine.compute(dict(FULL, fin_annual_revenue="250000000", lab_labor_pct="60"))
    assert r["categories"]["labor"]["status"] == "ok" and r["categories"]["labor"]["high"] > 0
    json.dumps(r)


def test_missing_data_never_lowers_score():
    partial = engine.compute({"fin_annual_revenue": "2000000", "lab_labor_pct": "33"})
    assert partial["scores"]["labor"]["score"] is None  # one signal isn't an assessment
    two = engine.compute({"fin_annual_revenue": "2000000", "lab_labor_pct": "33", "lab_know_daily": "yes"})
    assert two["scores"]["labor"]["score"] == 100
    assert two["scores"]["food"]["score"] is None


def test_technology_never_counts_pos_payroll_accounting():
    r = engine.compute(FULL)
    t = r["categories"]["technology"]
    assert t["likely"] == round(150 * 12 * 0.75, -2)
    assert "POS" in t["calc"]["assumptions"][0]


def test_pricing_follows_recommended_modules_and_override():
    r = engine.compute(FULL)
    assert r["plan"]["plan"] == "full" and r["plan"]["annual"] == 11990 and r["plan"]["setup"] == 3000
    one = engine.compute({"fin_annual_revenue": "2000000", "lab_labor_pct": "36", "lab_know_daily": "no"})
    assert one["plan"]["plan"] == "starter" and one["plan"]["annual"] == 3490 and one["plan"]["setup"] == 750
    two = engine.compute(FULL, pricing_override="starter:2")
    assert two["plan"]["annual"] == 6980 and two["plan"]["verify"]
    assert engine.compute(FULL, pricing_override="full")["plan"]["plan"] == "full"


def test_roi_uses_ranges_against_annual_investment():
    r = engine.compute(FULL)
    assert r["roi"]["low_x"] == round(r["totals"]["annual"]["low"] / 11990, 1)
    assert "does not guarantee" in r["disclaimer"]


def test_derived_values_carry_provenance():
    r = engine.compute({"fin_monthly_revenue": "100000", "lab_labor_dollars": "7000", "lab_labor_period": "Week"})
    f = r["financials"]
    assert f["annual_revenue"]["value"] == 1200000 and f["annual_revenue"]["source"] == "calculated"
    assert f["labor_pct"]["source"] == "calculated" and round(f["labor_pct"]["value"], 2) == round(7000 * 52 / 1200000 * 100, 2)


def test_conditional_questions_and_completion():
    assert not is_visible(QUESTIONS["lab_ot_hours_week"], {})
    assert is_visible(QUESTIONS["lab_ot_hours_week"], {"lab_overtime": "yes"})
    done, total, per = completion({"lab_overtime": "no"})
    done2, total2, _ = completion({"lab_overtime": "yes"})
    assert total2 == total + 2  # the two numeric follow-ups appear
    d3, _, _ = completion({"_unknown": ["lab_labor_pct"]})
    assert d3 == 1  # asked and unknown counts as answered


# ── storage ──────────────────────────────────────────────────────────────────

def test_create_save_resume_and_isolation(db_path):
    a = store.create_audit(answers={"restaurant_name": "A", "owner_name": "Ann"})
    b = store.create_audit(answers={"restaurant_name": "B", "owner_name": "Bob"})
    store.save_audit(a, answers={"restaurant_name": "A", "owner_name": "Ann", "fin_annual_revenue": "1000000"}, notes={"labor": {"internal": "secret"}})
    ra, rb = store.get_audit(a), store.get_audit(b)
    assert ra["answers"]["fin_annual_revenue"] == "1000000" and "fin_annual_revenue" not in rb["answers"]
    assert rb["notes"] == {} and ra["notes"]["labor"]["internal"] == "secret"
    assert ra["restaurant_name"] == "A" and rb["owner_name"] == "Bob"
    assert ra["status"] == "Draft"  # only three answers — not "In Progress" yet


def test_version_conflict_is_refused_with_current_copy(db_path):
    aid = store.create_audit(answers={"restaurant_name": "V"})
    row = store.get_audit(aid)
    saved = store.save_audit(aid, answers={"restaurant_name": "V", "x": "1"}, expected_version=row["version"])
    with pytest.raises(store.VersionConflict) as ex:
        store.save_audit(aid, answers={"restaurant_name": "V", "x": "stale"}, expected_version=row["version"])
    assert ex.value.current["answers"]["x"] == "1" and ex.value.current["version"] == saved["version"]


def test_duplicate_archive_delete(db_path):
    aid = store.create_audit(answers={"restaurant_name": "Dup", "fin_annual_revenue": "5"})
    nid = store.duplicate_audit(aid)
    assert store.get_audit(nid)["answers"]["fin_annual_revenue"] == "5"
    assert store.get_audit(nid)["restaurant_name"].endswith("(copy)")
    assert len(store.list_audits()) == 2
    store.archive_audit(aid)
    assert [r["id"] for r in store.list_audits()] == [nid]
    assert len(store.list_audits(include_archived=True)) == 2
    store.delete_audit(aid)
    assert store.get_audit(aid) is None


def test_search(db_path):
    store.create_audit(answers={"restaurant_name": "Simple EJ's", "owner_name": "Erik", "city": "Bozeman"})
    store.create_audit(answers={"restaurant_name": "Other", "owner_name": "Pat"})
    assert [r["restaurant_name"] for r in store.list_audits(q="erik")] == ["Simple EJ's"]
    assert [r["restaurant_name"] for r in store.list_audits(q="boze")] == ["Simple EJ's"]


def test_first_audit_seed_runs_once(db_path):
    aid = store.ensure_first_audit()
    a = store.get_audit(aid)
    assert a["restaurant_name"] == "Simple EJ's" and a["answers"]["restaurant_type"] == "Upscale sports bar"
    assert "fin_annual_revenue" not in a["answers"]  # no financial values assumed
    assert store.ensure_first_audit() is None
    store.archive_audit(aid)
    assert store.ensure_first_audit() is None


def test_public_view_strips_internal_material(db_path):
    aid = store.create_audit(answers=dict(FULL))
    store.save_audit(aid, notes={"labor": {"audit": "kept", "audit_in_report": True, "internal": "NEVER"}, "food": {"audit": "unticked"}},
                     sales={"objections": "price", "close_probability": "40"})
    store.store_results(aid, engine.compute(FULL), mark_generated=True)
    pv = store.public_view(store.get_audit(aid))
    blob = json.dumps(pv)
    assert "NEVER" not in blob and "objections" not in blob and "close_probability" not in blob
    assert pv["audit_notes"] == {"labor": "kept"}
    assert "answers" not in pv and "formula" not in blob and "reasons" not in blob


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def app(db_path):
    """A small app with the audit and auth blueprints — importing
    hosted_dashboard.py here is unsafe (real DB init, background threads),
    same reasoning as tests/test_admin_routes.py."""
    from flask import Flask
    from auth_routes import auth_bp
    from csrf import csrf_protect
    from sales_audit_routes import audit_bp
    flask_app = Flask(__name__, template_folder="../templates", static_folder="../static")
    flask_app.config["TESTING"] = True
    if not getattr(audit_bp, "_csrf_wired", False):
        csrf_protect(audit_bp)
        audit_bp._csrf_wired = True
    flask_app.register_blueprint(audit_bp)
    flask_app.register_blueprint(auth_bp)
    return flask_app


def _admin_client(app, db_path, monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "username": "will", "is_admin": 1, "restaurant_id": 1})
    c = app.test_client()
    c.set_cookie("csrf_js", "tok")
    return c


def _hdr():
    return {"X-CSRF": "tok"}


def test_routes_require_admin(app, db_path):
    c = app.test_client()
    assert c.get("/admin/audits").status_code in (302, 401)
    assert c.get("/admin/api/audits").status_code in (302, 401)
    assert c.get("/admin/audits/1/cheatsheet").status_code in (302, 401)


def test_full_workflow_over_http(app, db_path, monkeypatch):
    c = _admin_client(app, db_path, monkeypatch)
    r = c.post("/admin/api/audits", json={"restaurant_name": "HTTP Grill", "owner_name": "Erik", "restaurant_type": "Upscale sports bar"}, headers=_hdr())
    assert r.status_code == 200, r.data
    aid = r.get_json()["id"]
    g = c.get("/admin/api/audits/%d" % aid).get_json()
    assert g["ok"] and g["audit"]["answers"]["restaurant_name"] == "HTTP Grill"
    ver = g["audit"]["version"]
    # autosave with version
    s = c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "answers": dict(FULL, restaurant_name="HTTP Grill")}, headers=_hdr()).get_json()
    assert s["ok"] and s["version"] == ver + 1 and s["results"]["totals"]["annual"]["high"] > 0
    # stale version → conflict with the current copy
    st = c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "answers": {"restaurant_name": "stale"}}, headers=_hdr())
    assert st.status_code == 409 and st.get_json()["conflict"] and st.get_json()["audit"]["answers"]["restaurant_name"] == "HTTP Grill"
    # share before generate is refused
    assert c.post("/admin/api/audits/%d/share" % aid, headers=_hdr()).status_code == 400
    gen = c.post("/admin/api/audits/%d/generate" % aid, headers=_hdr()).get_json()
    assert gen["ok"] and gen["results"]["health"]["score"] is not None
    assert store.get_audit(aid)["status"] == "Completed"
    # internal notes must not reach the report
    c.patch("/admin/api/audits/%d" % aid, json={"notes": {"labor": {"internal": "SECRETNOTE", "audit": "visible", "audit_in_report": True}}, "sales": {"objections": "OBJ"}}, headers=_hdr())
    c.post("/admin/api/audits/%d/generate" % aid, headers=_hdr())
    rep = c.get("/admin/audits/%d/report" % aid)
    assert rep.status_code == 200 and b"SECRETNOTE" not in rep.data and b"OBJ" not in rep.data and b"visible" in rep.data
    sh = c.post("/admin/api/audits/%d/share" % aid, headers=_hdr()).get_json()
    assert sh["ok"]
    pub = app.test_client().get("/audit/r/" + sh["token"])
    assert pub.status_code == 200 and b"HTTP Grill" in pub.data and b"SECRETNOTE" not in pub.data and b"Back to audit" not in pub.data
    assert app.test_client().get("/audit/r/nope").status_code == 404
    c.delete("/admin/api/audits/%d/share" % aid, headers=_hdr())
    assert app.test_client().get("/audit/r/" + sh["token"]).status_code == 404
    # cheat sheet renders, is internal, and never appears in the report
    cs = c.get("/admin/audits/%d/cheatsheet" % aid)
    assert cs.status_code == 200 and b"Read this 5 minutes before" in cs.data and b"Erik" in cs.data
    assert b"Read this 5 minutes" not in rep.data
    # second audit is fully separate
    r2 = c.post("/admin/api/audits", json={"restaurant_name": "Second"}, headers=_hdr()).get_json()
    g2 = c.get("/admin/api/audits/%d" % r2["id"]).get_json()
    assert "fin_annual_revenue" not in g2["audit"]["answers"] and g2["results"]["totals"]["annual"]["high"] == 0
    g1 = c.get("/admin/api/audits/%d" % aid).get_json()
    assert g1["audit"]["answers"]["fin_annual_revenue"] == "2400000"
    # delete only after archive
    assert c.delete("/admin/api/audits/%d" % r2["id"], headers=_hdr()).status_code == 400
    c.post("/admin/api/audits/%d/status" % r2["id"], json={"status": "Archived"}, headers=_hdr())
    assert c.delete("/admin/api/audits/%d" % r2["id"], headers=_hdr()).status_code == 200
    # list + search
    lst = c.get("/admin/api/audits?q=http").get_json()
    assert [a["restaurant_name"] for a in lst["audits"]] == ["HTTP Grill"]
    assert lst["audits"][0]["opportunity"]["high"] > 0


def test_csrf_required_for_saves(app, db_path, monkeypatch):
    c = _admin_client(app, db_path, monkeypatch)
    aid = store.create_audit(answers={"restaurant_name": "C"})
    r = c.patch("/admin/api/audits/%d" % aid, json={"answers": {"restaurant_name": "X"}})
    assert r.status_code == 403
    assert store.get_audit(aid)["answers"]["restaurant_name"] == "C"


def test_latest_redirects_for_admin_console_buttons(app, db_path, monkeypatch):
    c = _admin_client(app, db_path, monkeypatch)
    r = c.get("/admin/audits/latest")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/audits")
    aid = store.create_audit(answers={"restaurant_name": "Latest"})
    assert c.get("/admin/audits/latest").headers["Location"].endswith("/admin/audits/%d" % aid)
    assert c.get("/admin/audits/latest/cheatsheet").headers["Location"].endswith("/admin/audits/%d/cheatsheet" % aid)
