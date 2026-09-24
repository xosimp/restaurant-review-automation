"""The web clients read the fields the server groups (E-I of the confidence
audit) actually emit, under their real names.

Group J built the web side before the server existed and guessed some
names; this pins each client read to the server's name AND checks the
server still emits it, so a rename on either side fails here rather than
silently rendering nothing. The pure helpers (fc2AccLine,
laborSplitUnverified) run under node against payloads shaped like the
server's."""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")
ADMIN = os.path.join(ROOT, "templates", "admin.html")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _dash():
    return _read("templates", "dashboard.html")


def _admin():
    return _read("templates", "admin.html")


# (field, the server file that emits it, the template that reads it)
CONTRACT = [
    ("ai_score_label", "client_api.py", "dashboard"),
    ("ai_score_tone", "client_api.py", "dashboard"),
    ("ai_score_band", "client_api.py", "dashboard"),
    ("presence_band_label", "client_api.py", "dashboard"),
    ("presence_tone", "client_api.py", "dashboard"),
    ("intel_market", "hosted_dashboard.py", "dashboard"),
    ("standing_label", "competitor_intel_format.py", "dashboard"),
    ("own_vs_market", "competitor_intel_format.py", "dashboard"),
    ("market_rating_n", "competitor_intel_format.py", "dashboard"),
    ("labor_industry_pct", "hosted_dashboard.py", "dashboard"),
    ("labor_industry_basis", "hosted_dashboard.py", "dashboard"),
    ("benchmark_state", "inventory.py", "dashboard"),
    ("recoverable_kind", "inventory.py", "dashboard"),
    ("annual_recoverable_basis", "inventory.py", "dashboard"),
    ("rating_note", os.path.join("dsr", "block_reviews.py"), "dashboard"),
    ("coverage_note", os.path.join("dsr", "block_food.py"), "dashboard"),
    ("urgency_adjusted", os.path.join("dsr", "narrative.py"), "dashboard"),
    ("urgency_basis", os.path.join("dsr", "narrative.py"), "dashboard"),
    ("calibration_note", "schedule_learning.py", "dashboard"),
    ("assumption", "schedule_learning.py", "dashboard"),
    ("no_show_threshold", "staff_settings.py", "dashboard"),
    ("raw_no_show_rate", "staff_settings.py", "dashboard"),
    ("unreliable", "staff_settings.py", "dashboard"),
    ("prime_cost_accuracy", "food_cost_intelligence.py", "dashboard"),
    ("forecast_low", os.path.join("dsr", "block_sales.py"), "dashboard"),
    ("consistent_monthly", "value_delivered.py", "dashboard"),
    ("associated_monthly", "value_delivered.py", "dashboard"),
    ("grade_phrase", "outcomes.py", "dashboard"),
    ("baseline_overlaps_trigger", "outcomes.py", "dashboard"),
    ("band_basis", "outcomes.py", "dashboard"),
    ("issues_label", "schedule_intel.py", "dashboard"),
    ("item_verdict", "marketing_signals.py", "dashboard"),
    ("change_note", "marketing_signals.py", "dashboard"),
    ("causes_verified", "client_api.py", "dashboard"),
    ("unsupported_causes", "client_api.py", "dashboard"),
    ("stale_note", "client_api.py", "dashboard"),
    ("needs_yield", "recipes.py", "dashboard"),
    ("unit_warnings", "recipes.py", "dashboard"),
    ("confidence_levels", "recipes.py", "dashboard"),
    ("unit_skipped", "recipes.py", "dashboard"),
    ("unit_ok", "recipes.py", "dashboard"),
    ("card_qty", "recipes.py", "dashboard"),
    ("is_estimate", "recipes.py", "dashboard"),
    ("strength_pct", os.path.join("intelligence", "patterns.py"), "admin"),
    ("pos_state", "admin_ops.py", "admin"),
    ("sync_state", "admin_ops.py", "admin"),
]


@pytest.mark.parametrize("field,server,client", CONTRACT, ids=[c[0] for c in CONTRACT])
def test_every_field_the_client_reads_is_one_the_server_emits(field, server, client):
    assert re.search(r"\b" + re.escape(field) + r"\b", _read(server)), f"{server} no longer emits {field}"
    src = _dash() if client == "dashboard" else _admin()
    assert re.search(r"\b" + re.escape(field) + r"\b", src), f"{client} does not read {field}"


def test_guessed_names_are_gone():
    src = _dash()
    # Group J's guesses, replaced by what the server sends.
    for guess in ("likely_edits_note", "standby_note", "event.date_iso", "event.basis"):
        assert guess not in src, guess
    # presence_label is the metric's NAME ("Listing strength"), never its band.
    assert "var presLabel = d.presence_label" not in src
    # The per-row threshold is read from the row, not only the payload root.
    assert "r.no_show_threshold" in src


def test_not_measured_waste_never_reads_as_on_track():
    src = _dash()
    i_state = src.index("inv.benchmark_state == 'not_measured'")
    i_good = src.index("{% elif _wl in ['Excellent','On Track'] %}")
    assert i_state < i_good, "the not-measured branch must be decided before the good one"
    assert "you can claw back" not in src


def test_labor_industry_benchmark_is_the_servers_not_32_or_35():
    src = _dash()
    assert "Vs 32% avg" not in src and "% vs 33–36% full-service average" not in src
    assert "savings_breakdown.labor_industry_pct" in src


def test_daypart_with_no_watched_night_never_says_no_issues():
    src = _dash()
    line = next(l for l in src.split("\n") if "issues_label" in l and "no issues" in l)
    # issues_label first, then an explicit null check, and only then the old wording.
    assert line.index("o.issues_label") < line.index("o.issues==null") < line.index("' · no issues'")


def test_value_parts_by_grade_are_said_apart_never_summed():
    src = _dash()
    seg = src[src.index("var cm=+d.consistent_monthly"):]
    seg = seg[:seg.index("\n    if(d.validated>0)")]
    assert "cm+am" not in seg.replace(" ", "") and "am+cm" not in seg.replace(" ", "")
    assert "associated, not proven" in seg


def test_patterns_say_strength_not_confidence():
    a = _admin()
    assert "Highest-confidence insights" not in a
    assert "{h:'Conf.'" not in a
    assert "pattern strength" in a


def _fn_src(src, name, indent=""):
    m = re.search(r"\n" + indent + r"function " + re.escape(name) + r"\(.*?\n" + indent + r"\}", src, re.S)
    assert m, name
    return m.group(0)


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_forecast_accuracy_line_below_the_floor_says_what_it_needs():
    fn = _fn_src(_dash(), "fc2AccLine")
    below = {"available": False, "kind": "profitability_month", "scored": 1, "n_months": 1, "reading": None,
             "withheld": False, "reason": "not enough scored forecasts yet to say how accurate these are — "
                                          "1 closed month scored, needs 3"}
    good = {"available": True, "reading": "close", "mean_error_pct": 6.2, "bias_pct": 7.5, "n_weeks": 5,
            "withheld": False}
    held = {"available": True, "reading": "often wide", "mean_error_pct": 41.0, "n_weeks": 4, "withheld": True,
            "reason": "past forecasts here missed by 41% on average over 4 weeks, so the next one is not shown"}
    got = _node("function wtEsc(x){return String(x==null?'':x);}\n" + fn + "\nconsole.log(JSON.stringify(["
                + ",".join(f"fc2AccLine({json.dumps(x)},'forecasts')" for x in (below, good, held, None))
                + "]));")
    txt = [re.sub(r"<[^>]+>", "", g) for g in got]
    assert "needs 3" in txt[0] and "%" not in txt[0]
    assert "close" in txt[1] and "6.2% mean error" in txt[1] and "5 weeks" in txt[1] and "7.5% high" in txt[1]
    assert "held back" in txt[2] and "41%" in txt[2]
    assert got[3] == ""


def test_labor_unverified_structured_note_is_said_whole():
    fn = _fn_src(_dash(), "laborSplitUnverified")
    note = ("figures not in the data: $400; a cause no stored diagnosis supports "
            "(\"Labor rose because of the patio\")")
    got = _node(fn + "\nconsole.log(JSON.stringify([laborSplitUnverified(" + json.dumps("Labor ran 34%.\n\nUNVERIFIED: " + note)
                + "), laborSplitUnverified('Labor ran 34%.\\n\\nUNVERIFIED: 12%, $400')]));")
    assert got[0]["figs"] is None and got[0]["note"] == note and got[0]["html"] == "Labor ran 34%."
    assert got[1]["figs"] == ["12%", "$400"] and got[1]["note"] is None


# ── the Jinja side renders against the server's own payloads ───────────────

RENDER = r'''
import json, os, re, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()   # pre-create: no legacy adoption
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Full Co", owner_email="full@x.test", module_reviews=1,
                                          module_labor=1, module_inventory=1, module_marketing=1))
models.update_restaurant(rid, {"google_place_id": "ChIJfull", "gbp_rating": 4.8, "gbp_review_count": 300,
    "competitor_intel": json.dumps({"insight": "Hi.", "competitors": [
        {"name": "A", "rating": 4.4, "review_count": 200, "reviews": [], "vicinity": "Main St"}, {"name": "B", "rating": 4.2, "review_count": 100, "reviews": [], "vicinity": "Main St"},
        {"name": "Tiny", "rating": 5.0, "review_count": 3, "rating_is_provisional": True, "reviews": [], "vicinity": "Main St"}]})})
uid = auth.create_user(rid, "full", "full@x.test", "correct-horse-battery", is_admin=False)
c = h.app.test_client()
page = c.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "full", "password": "correct-horse-battery", "csrf_token": token})
assert r.status_code in (302, 303), r.status_code
r = c.get("/")
assert r.status_code == 200, r.get_data(as_text=True)[-800:]
html = r.get_data(as_text=True)
print(json.dumps({"lead": "You lead the block" in html, "all_time": "your Google rating (all time)" in html,
                  "weighted": "weighted by reviews" in html, "industry": "industry benchmark" in html,
                  "claw": "claw back" in html, "vs32": "Vs 32% avg" in html}))
'''


def test_intel_and_labor_render_from_the_servers_definitions():
    import sys
    import tempfile
    vol = tempfile.mkdtemp(prefix="cavnar-integ-")
    out = subprocess.run([sys.executable, "-c", RENDER, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    # 4.8 all-time against a review-weighted 4.3 (the 3-review 5.0 is left
    # out): the server's standing, worded by the server.
    assert got["lead"] and got["all_time"] and got["weighted"], got
    assert got["industry"] and not got["vs32"] and not got["claw"], got
