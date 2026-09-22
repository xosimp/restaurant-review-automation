"""A restaurant with the Labor module and no shift data yet — every new
signup — must be shown no labor figures at all. load_shifts_for_restaurant
falls back to a bundled June 2026 sample week and analyse_shifts_for_restaurant
marks it is_live=False; the surfaces below do not check that flag, so a
brand-new owner is told the fixture's "36.8% labor, $12,630/mo over target"
as their own (MOD-LAB-16, P0).

Deliberately no monkeypatch of models._cached_shifts or the loader: the
fallback itself is what is under test. Confirmed defects are strict xfails."""
import os
import sys

import pytest
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, WeeklyReport, create_restaurant

CSRF = "edge-mod-a-sample-csrf"



# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, delayed, demand, food_cost_intelligence, intraday, inventory, inventory_ledger  # noqa: E401,F401
import invoices, issues, marketing_signals, notify, ops, pos, push, recipes, reporter  # noqa: E401,F401
import staff_settings, strategy_jobs, toast, square, clover, webhooks  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    # Every repo module holding a get_conn: the real one by identity, and any
    # stale redirect an earlier test's lazy import bound (CLAUDE.md "Bound imports").
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))


@pytest.fixture
def fresh(db_path):
    """A labor customer on day one: module on, nothing uploaded, no POS."""
    rid = create_restaurant(Restaurant(name="Day One Diner", owner_email="new@owner.test",
                                       module_reviews=1, module_labor=1), db_path=db_path)
    return rid


def test_the_analysis_itself_says_it_is_not_live(fresh):
    a = labor.analyse_shifts_for_restaurant(fresh)
    assert a["is_live"] is False


def test_the_monthly_gap_is_projectable_from_the_sample_week_today(fresh):
    """Pins the mechanism the defects below ride on: the sample week alone
    yields a confident monthly overspend."""
    gap = labor.calculate_monthly_gap(labor.analyse_shifts_for_restaurant(fresh))
    assert gap["projectable"] is True and gap["monthly_gap"] > 0


def test_the_labor_gap_route_projects_nothing_without_shift_data(db_path, fresh):
    app = Flask(__name__, template_folder="/Users/simp/review_automation/templates")
    app.register_blueprint(client_api.client_bp)
    uid = create_user(fresh, "owner", "new@owner.test", "pw123456", db_path=db_path)
    upsert_membership(uid, fresh, "client", db_path=db_path)
    cl = app.test_client()
    cl.set_cookie("csrf_js", CSRF)
    cl.set_cookie("session_token", create_session(uid, db_path=db_path))
    body = cl.get("/api/labor-gap").get_json()
    assert not body.get("projectable")
    assert not body.get("monthly_gap")


def test_the_mobile_home_labor_kpi_shows_no_figure_without_shift_data(fresh):
    user = {"id": 1, "restaurant_id": fresh, "base_restaurant_id": fresh, "role": "client",
            "is_admin": 0, "username": "owner", "email": "new@owner.test"}
    payload, status = mobile_api._do_mobile_home(user)
    assert status == 200
    labor_mod = [m for m in payload.get("modules", []) if m.get("key") == "labor"]
    kpi = (labor_mod[0].get("kpi") if labor_mod else None) or {}
    assert "%" not in str(kpi.get("value") or "")
    assert "target" not in str(kpi.get("sublabel") or "")


def test_the_weekly_digest_has_no_labor_figures_without_shift_data(fresh, monkeypatch):
    import reporter
    monkeypatch.setattr(reporter, "generate_ai_digest_summary", lambda *a, **k: {"headline": "A quiet week."})
    report = WeeklyReport(restaurant_id=fresh, period_start="2026-09-14", period_end="2026-09-20",
                          total_reviews=0, avg_rating=0.0,
                          sentiment={"positive": 0, "negative": 0, "neutral": 0}, top_issues=[])
    html = reporter.render_html(report, "Day One Diner", owner_name="Pat", restaurant_id=fresh)
    assert "Labor Optimizer" not in html
    assert "labor ratio" not in html


def test_post_attribution_reads_no_pos_sales_without_shift_data(fresh):
    import marketing_signals
    assert marketing_signals.daily_sales(fresh) == {}


def test_after_a_real_upload_the_surfaces_read_the_owners_own_week(db_path, fresh):
    csv_text = "date,employee,role,actual_hours,sales\n" + "".join(
        f"2026-09-{d:02d},Ann,Server,8,2000\n" for d in range(14, 21))
    models.save_client_data(fresh, "shifts", csv_text, db_path=db_path)
    a = labor.analyse_shifts_for_restaurant(fresh)
    assert a["is_live"] is True and a["total_sales"] == 14000.0
    import marketing_signals
    assert marketing_signals.daily_sales(fresh)["2026-09-14"] == 2000.0
