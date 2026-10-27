"""The iPhone app's top GET payloads, captured from the real routes on a
fully synthetic restaurant (release-safety audit #25: A04-04, A06-03,
A05-14) — the harness behind tests/test_ios_response_shapes.py.

The restaurant is Halsted & Rye, the showcase demo (demo_store): a fictional
RPOWER store whose POS answers RPOWER's own API, so the POS archive, the
Daily Sales Report, Labor, Food Cost and the Bar are built by the real
pipelines, and the reviews, purchasing, marketing and neighborhood through
each module's own import path. Nothing in it is a client's: names, numbers,
addresses (.example, 555-01xx) are invented (demo_store/__init__.py). It never
sends and never calls out (demo_store.is_simulated). Its days are the
restaurant's own today, so the payloads are always fresh; their content
changes from day to day, their shape does not.

The only model answer is the night's DSR narrative — written from the
night's own facts, every figure cited, so it survives the narrative's checks
and the report carries a real narrative block. Every other model call is
refused (no key in the suite), the way a fresh restaurant's reads look.
"""
import json
import sys
import types
from datetime import timedelta

from flask import Flask

# What the routes and the build import lazily, imported before the
# get_conn sweep so every bound copy is redirected
# (test_ios_schedule_payload_contract's rule).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_versions, shift_requests, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401
import analyser, ask_cavnar, benchmark_registry, business_intelligence, clover, cogs  # noqa: E401,F401
import command_center, credentials, data_freshness, data_health, demand, food_cost_intelligence  # noqa: E401,F401
import forecast_log, home_brief, inventory, inventory_ledger, kitchen_stations, marketing  # noqa: E401,F401
import marketing_signals, morning_brief, ordering, owner_memory, permissions, pos, pos_health  # noqa: E401,F401
import rec_trust, reservation_feeds, review_intelligence, rpower, schedule_learning  # noqa: E401,F401
import schedule_optimizer, schedule_solver, square, staffing_curve, staffing_signals, toast  # noqa: E401,F401
import waste_trend, weather, pos_archive, loss_detection, fintech_invoices, backoffice_ap  # noqa: E401,F401
import backoffice_recipes, web_analytics, event_memory, value_delivered, closeout, labor  # noqa: E401,F401
import home_snapshots, staff_insights, staff_brief, task_sheets, bar_cost, bar_signals  # noqa: E401,F401
import dsr.pipeline, dsr.backfill, dsr.narrative, dsr.store, dsr.access, dsr.rollup  # noqa: E401,F401
import bar.counts, bar.pour_cost  # noqa: E401,F401
import ai_utils
import auth
import mobile_api
import models
import staff_routes
import strategy_routes
import demo_store
from demo_store import api as demo_api, build as demo_build

# The employee whose shifts the staff app's screens read: a fictional
# server on the demo store's roster, published by fill_schedules.
STAFF_NAME = "Kayla Brennan"
OWNER = ("halstedowner", "owner@halstedandrye.example")
MANAGER = ("halstedmanager", "manager@halstedandrye.example")
STAFF = ("halstedstaff", "kayla@staff.invalid")
PASSWORD = "Halsted-Rye-2026"

# route name -> (owner|manager|staff, path, query): what each screen reads,
# with the query its view model sends.
ROUTES = {
    "me": ("owner", "/mobile/api/me", {}),
    "account": ("owner", "/mobile/api/account", {}),
    "home": ("owner", "/mobile/api/home", {}),
    "reviews": ("owner", "/mobile/api/reviews", {"filter": "all", "limit": "30", "offset": "0"}),
    "notifications": ("owner", "/mobile/api/notifications", {"mark": "0", "scope": "group"}),
    "labor": ("owner", "/mobile/api/labor", {}),
    "food_cost": ("owner", "/mobile/api/food-cost/analytics", {}),
    "food_cost_bar": ("owner", "/mobile/api/food-cost/bar", {}),
    "marketing": ("owner", "/mobile/api/marketing", {}),
    "intel": ("owner", "/mobile/api/intel", {}),
    "actions": ("owner", "/mobile/api/actions", {}),
    "staff_shifts": ("staff", "/staff/api/shifts", {}),
    "staff_me": ("staff", "/staff/api/me", {}),
}
# Two views of one report each: the owner's and the manager's (dsr.access).
DSR_ROUTES = ("dsr_preview", "dsr_week")


def _sweep(monkeypatch, db_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)


# ── the night's narrative, from its own facts ──────────────────────────────

def _prompt_figures(prompt):
    """{"sales.net": "$6,975", ...}: the night's facts as the prompt lists
    them ("sales.net = $6,975"), in the prompt's own formatting."""
    out = {}
    for line in str(prompt or "").splitlines():
        key, sep, value = line.partition(" = ")
        if sep and "." in key and " " not in key.strip():
            out[key.strip()] = value.strip()
    return out


def narrative_for(figures):
    """A narrative the checks keep: figures quoted from the prompt's own
    facts, each cited; the singles and an action, so every block of the
    narrative the phone reads is present."""
    net, pct = figures.get("sales.net"), figures.get("labor.pct")

    def item(text, *cites):
        return {"text": text, "cites": [c for c in cites if c in figures] or ["sales.net"]}
    lead = (f"Net sales were {net}." if net else "Sales came in.") + (f" Labor ran {pct}." if pct else "")
    return {
        "executive_summary": item(lead + " Keep the dinner pattern for the next schedule.", "sales.net", "labor.pct"),
        "operations_summary": item((f"Net sales were {net}." if net else "Sales came in.")
                                   + " The floor kept up through dinner.", "sales.net"),
        "went_well": [item("The dinner rush carried the night.", "sales.net")],
        "needs_attention": [item("Check the close-out notes before the next shift.", "sales.net")],
        "biggest_win": item("Dinner carried the night.", "sales.net"),
        "highest_priority_issue": item("Keep the close-out notes current.", "sales.net"),
        "actions_tomorrow": [{"text": "Keep the same dinner staffing for the next schedule.",
                              "why": "Dinner carried the night.", "dollars_monthly": None,
                              "urgency": "next_schedule", "effort": "low", "kind": "adjust_staffing",
                              "subject": None, "cites": ["sales.net"]}],
    }


class _NarrativeClient:
    """ai_utils.get_client's stand-in while the nights are written: one
    answer per call, written from the figures its own prompt lists."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        content = (kw.get("messages") or [{}])[0].get("content")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        text = json.dumps(narrative_for(_prompt_figures(content)))
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                                     usage=types.SimpleNamespace(input_tokens=3000, output_tokens=600,
                                                                 cache_creation_input_tokens=0,
                                                                 cache_read_input_tokens=0))


# ── the world ───────────────────────────────────────────────────────────────

def build_world(monkeypatch, db_path):
    """The demo store in `db_path`, a little shorter than the showcase's
    (weeks, not a year), with an owner, a manager and one employee login."""
    _sweep(monkeypatch, db_path)
    rid = demo_build.ensure_account()
    demo_build.fill_settings(rid)
    demo_build.fill_pos(rid, archive_days=8, history_days=42)
    demo_build.fill_schedules(rid, weeks=2, ahead=1)
    demo_build.fill_purchasing(rid, days=42)
    demo_build.fill_bar(rid)
    demo_build.fill_kitchen(rid, weeks=3)
    demo_build.fill_reviews(rid, days=60)
    demo_build.fill_neighborhood(rid, weeks=3)
    demo_build.fill_marketing(rid, weeks=4)
    demo_build.fill_people(rid)
    demo_build.fill_closeouts(rid, nights=5)
    _seed_notifications(rid)
    with monkeypatch.context() as m:
        m.setattr(ai_utils, "get_client", lambda *a, **k: _NarrativeClient())
        demo_build.fill_dsr(rid, nights=2, history_days=42)
    owner = auth.create_user(rid, OWNER[0], OWNER[1], PASSWORD)
    manager = auth.create_user(rid, MANAGER[0], MANAGER[1], PASSWORD)
    auth.set_user_role(manager, "manager")
    staff_uid = auth.create_user(rid, STAFF[0], STAFF[1], PASSWORD)
    membership = auth.upsert_membership(staff_uid, rid, "employee", employee_name=STAFF_NAME)
    auth.set_membership_pin(membership["id"], rid, "8317")
    tokens = {"owner": auth.create_session(owner, device_type="ios", restaurant_id=rid),
              "manager": auth.create_session(manager, device_type="ios", restaurant_id=rid),
              "staff": auth.create_staff_session(staff_uid, rid)}
    return rid, tokens


def _seed_notifications(rid):
    """A few rows on the bell, as the alert jobs write them (notify._log_alert):
    a low review and a five-star one (each joined to its review), labor over
    target, a critical-low item and the night's report."""
    import notify
    conn = models.get_conn()
    try:
        low = conn.execute("SELECT id FROM reviews WHERE restaurant_id=? ORDER BY rating, id LIMIT 1",
                           (rid,)).fetchone()
        high = conn.execute("SELECT id FROM reviews WHERE restaurant_id=? ORDER BY rating DESC, id LIMIT 1",
                            (rid,)).fetchone()
    finally:
        conn.close()
    notify._log_alert(rid, "2star", review_id=low["id"])
    notify._log_alert(rid, "5star", review_id=high["id"])
    notify._log_alert(rid, "labor_over", value=31.5)
    notify._log_alert(rid, "critical_low", value=2)
    notify._log_alert(rid, "dsr")


def _app():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    app.register_blueprint(staff_routes.staff_bp)
    return app


def capture(monkeypatch, db_path):
    """{route name: payload} — dsr_preview and dsr_week as {"owner", "manager"}."""
    rid, tokens = build_world(monkeypatch, db_path)
    client = _app().test_client()
    out, problems = {}, []

    def get(who, path, query):
        r = client.get(path, query_string=query, headers={"Authorization": f"Bearer {tokens[who]}"})
        if r.status_code != 200:
            problems.append(f"{path} as {who}: {r.status_code} {r.get_data(as_text=True)[:300]}")
            return None
        return r.get_json()
    for name, (who, path, query) in ROUTES.items():
        out[name] = get(who, path, query)
    latest = get("owner", "/mobile/api/dsr", {"limit": "1"})
    day = (((latest or {}).get("reports") or [{}])[0] or {}).get("business_date")
    if not day:
        problems.append(f"no DSR night to read: {latest}")
    else:
        out["dsr_preview"] = {v: get(v, f"/mobile/api/dsr/{day}", {}) for v in ("owner", "manager")}
        out["dsr_week"] = {v: get(v, "/mobile/api/dsr/week", {"date": day}) for v in ("owner", "manager")}
    if problems:
        raise AssertionError("routes the phone reads did not answer:\n" + "\n".join(problems))
    return out
