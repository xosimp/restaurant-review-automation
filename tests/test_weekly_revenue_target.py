"""One revenue target, set by the month or by the week (owner, 9/28/26:
Erik at Simple EJ's plans by the week, not the month).

`monthly_revenue_target` stays the one stored figure; a weekly figure is
stored as weekly × 52/12 (metrics.WEEKS_PER_MONTH) and read back ÷ 52/12, so
what the owner typed returns to the cent and every reader of the monthly
moves with it."""
import json
import os
import re
import shutil
import subprocess

import pytest

import models
from metrics import WEEKS_PER_MONTH
from test_friction_o1 import db, web, _restaurant, _as  # noqa: F401  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("weekly", [50000, 50000.5, 84230.77, 1234.56, 19999999.99])
def test_a_weekly_target_comes_back_to_the_cent(db, weekly):
    rid = _restaurant(db)
    models.update_restaurant(rid, {"weekly_revenue_target": weekly}, db_path=db)
    r = models.get_restaurant(rid, db_path=db)
    assert r.monthly_revenue_target == pytest.approx(weekly * 52 / 12)
    assert models.weekly_revenue_target(r) == round(weekly, 2)


def test_blank_or_zero_weekly_clears_the_target_and_junk_is_refused(db):
    rid = _restaurant(db)
    models.update_restaurant(rid, {"weekly_revenue_target": 40000}, db_path=db)
    models.update_restaurant(rid, {"weekly_revenue_target": ""}, db_path=db)
    assert models.get_restaurant(rid, db_path=db).monthly_revenue_target == 0
    models.update_restaurant(rid, {"weekly_revenue_target": 40000}, db_path=db)
    models.update_restaurant(rid, {"weekly_revenue_target": 0}, db_path=db)
    assert models.weekly_revenue_target(models.get_restaurant(rid, db_path=db)) == 0
    with pytest.raises(ValueError):
        models.update_restaurant(rid, {"weekly_revenue_target": "lots"}, db_path=db)


def test_given_both_the_weekly_wins_and_a_monthly_alone_still_sets_it(db):
    rid = _restaurant(db)
    models.update_restaurant(rid, {"weekly_revenue_target": 50000, "monthly_revenue_target": 1}, db_path=db)
    assert models.weekly_revenue_target(models.get_restaurant(rid, db_path=db)) == 50000
    models.update_restaurant(rid, {"monthly_revenue_target": 365000}, db_path=db)
    r = models.get_restaurant(rid, db_path=db)
    assert r.monthly_revenue_target == 365000 and models.weekly_revenue_target(r) == round(365000 / WEEKS_PER_MONTH, 2)


def test_the_owner_sets_it_by_the_week_on_the_targets_card(db, web, monkeypatch):
    rid = _restaurant(db)
    _as(monkeypatch, rid, "client")
    c = web.test_client()
    r = c.post("/api/account/targets", json={"weekly_revenue_target": 50000})
    assert r.status_code == 200, r.get_json()
    t = r.get_json()["targets"]
    assert t["weekly_revenue_target"] == 50000
    assert t["monthly_revenue_target"] == pytest.approx(216666.67, abs=0.01)
    assert c.get("/api/account/targets").get_json()["targets"]["weekly_revenue_target"] == 50000
    # one target: a monthly sent beside it is ignored, the weekly decides
    t = c.post("/api/account/targets", json={"weekly_revenue_target": 60000,
                                             "monthly_revenue_target": 5}).get_json()["targets"]
    assert t["weekly_revenue_target"] == 60000
    # typed by the month, the weekly follows
    t = c.post("/api/account/targets", json={"monthly_revenue_target": 260000}).get_json()["targets"]
    assert t["weekly_revenue_target"] == 60000
    assert c.post("/api/account/targets", json={"weekly_revenue_target": -5}).status_code == 400
    assert c.post("/api/account/targets", json={"weekly_revenue_target": "x"}).status_code == 400


def test_the_readers_of_the_target_see_the_weekly_figure(db):
    rid = _restaurant(db)
    models.update_restaurant(rid, {"weekly_revenue_target": 49000}, db_path=db)
    r = models.get_restaurant(rid, db_path=db)
    # borrowed staffing spreads the WEEK over seven days
    from intelligence import staffing
    per_day, basis = staffing._own_sales_by_weekday(rid, r, db)
    assert per_day["Friday"] == pytest.approx(7000) and "weekly revenue target" in basis
    # Ask names it the way the owner thinks of it
    from ask_cavnar import build_context
    ctx = build_context(r)
    assert "Revenue target: $49,000 a week ($212,333 a month)" in ctx
    # the PAR budget's weekly projection is the weekly figure itself
    assert round(r.monthly_revenue_target / WEEKS_PER_MONTH, 0) == 49000


# ── The Account card: one row per unit, each fills the other as you type ──

SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    start = SRC.index("function " + name + "(")
    depth, i = 0, SRC.index("{", start)
    while True:
        if SRC[i] == "{":
            depth += 1
        elif SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:i + 1]
        i += 1


def test_the_weekly_row_sits_under_the_monthly_and_each_names_the_other():
    mo = SRC.index('id="as-tg-monthly_revenue_target"')
    wk = SRC.index('id="as-tg-weekly_revenue_target"')
    assert mo < wk < SRC.index('id="as-tg-hourly_rate"')
    assert re.search(r'id="as-tg-weekly_revenue_target"[^>]*data-tg="weekly_revenue_target"[^>]*'
                     r'data-tg-pair="monthly_revenue_target"', SRC)
    assert re.search(r'id="as-tg-monthly_revenue_target"[^>]*data-tg-pair="weekly_revenue_target"', SRC)


def test_the_live_math_rounds_for_display_only():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = "var TG_WPM = 52 / 12;\n" + _fn("tgRev") + """
    console.log(JSON.stringify([tgRev('monthly_revenue_target', 50000 * TG_WPM),
      tgRev('weekly_revenue_target', 365000 / TG_WPM), tgRev('weekly_revenue_target', 0),
      tgRev('monthly_revenue_target', null), tgRev('weekly_revenue_target', 50000)]));"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["216667", "84230.77", "", "", "50000"]


def test_the_revenue_target_is_set_in_one_place_the_owners_account():
    """The admin page repeated the owner's Account -> Targets boxes (owner,
    9/30/26); the owner's pair is the one on screen. The admin save still
    takes either (the route test below)."""
    admin = open(os.path.join(ROOT, "templates", "client_settings.html"), encoding="utf-8").read()
    assert 'id="weekly_revenue_target"' not in admin and 'id="monthly_revenue_target"' not in admin
    dash = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert 'id="as-tg-weekly_revenue_target"' in dash and 'id="as-tg-monthly_revenue_target"' in dash


def test_the_admin_save_takes_the_weekly_over_the_monthly_it_resent(db, monkeypatch):
    from flask import Flask
    import admin_routes
    import auth
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db)
    models.update_restaurant(rid, {"monthly_revenue_target": 365000}, db_path=db)
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)

    def save(**body):
        with app.test_request_context(f"/admin/client-settings/{rid}", method="POST",
                                      json=dict({"name": "People Co", "owner_email": "p@x.com"}, **body)):
            return admin_routes.save_client_settings(rid).get_json()

    # untouched: the monthly is re-sent exactly as stored and nothing drifts
    assert save(monthly_revenue_target=365000)["ok"]
    assert models.get_restaurant(rid, db_path=db).monthly_revenue_target == 365000
    # typed by the week: the form adds the weekly and it decides
    assert save(monthly_revenue_target=216667, weekly_revenue_target=50000)["ok"]
    assert models.weekly_revenue_target(models.get_restaurant(rid, db_path=db)) == 50000
