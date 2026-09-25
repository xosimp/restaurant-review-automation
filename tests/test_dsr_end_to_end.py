"""One DSR night end to end: the POS closes the day, every real collector
runs, the real narrative writes over the saved facts (a fake model client —
no test reaches a real model), and the owner and manager read their views
through the real routes.

Each piece has its own file (test_dsr_pipeline, test_dsr_blocks,
test_dsr_sales_labor, test_dsr_narrative, test_dsr_routes), each with fakes
for its neighbours. This one has only the two outside edges faked — the POS
and the model — so a seam between the pieces (a key one writes and another
reads under a different name) fails here.

Chicago, open 11am–11pm. Business date Tuesday 9/22/26 closes 04:00 UTC on
9/23; the run is at 04:10 UTC.
"""
import json
import sys
import types
from datetime import date, datetime

import pytest
from flask import Flask

import ai_utils
import auth
import closeout
import guest_marketing
import models
import ops
import pos
import dsr
from dsr import pipeline, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)
RUN_AT = datetime(2026, 9, 23, 4, 10)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


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
    auth.init_auth(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path)
    models.init_competitor_snapshots(db_path)
    ops.init_ops(db_path)
    ai_utils.reset_breaker()
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    monkeypatch.delenv("DSR_NARRATIVE_MODEL", raising=False)
    yield db_path
    ai_utils.reset_breaker()


# ── the two outside edges ───────────────────────────────────────────────────

SALES_DAY = {"gross": 2150.0, "net": 2000.0, "transactions": 80, "guests": 120, "discounts": 60.0, "comps": 40.0,
             "voids": 25.0, "refunds": 0.0, "tax": 160.0, "by_department": {"Food": 1500.0, "Liquor": 500.0},
             "by_hour": {"12": 800.0, "19": 1200.0}, "items": [], "net_deductions": ["discounts", "comps"],
             "source_checks": {}}


@pytest.fixture
def fake_pos(monkeypatch):
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("fakepos", object()))
    monkeypatch.setattr(pos, "fetch_day_closed", lambda rid, day: (True, "fakepos"))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: (dict(SALES_DAY), "fakepos"))


class _Client:
    def __init__(self, reply):
        self.reply, self.calls, self.messages = reply, [], self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.reply))], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=3000, output_tokens=800,
                                        cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _it(text, *cites):
    return {"text": text, "cites": list(cites)}


def _act(text, why, kind, cites, urgency="before_service"):
    return {"text": text, "why": why, "dollars_monthly": None, "urgency": urgency, "effort": "low", "kind": kind,
            "subject": None, "cites": list(cites)}


# Written the way the model writes: every figure cited, the lead two sentences.
REPLY = {
    "executive_summary": _it("Tuesday did $2,000 net on 80 checks. Labor ran 22% of sales against a 30% target.",
                             "sales.net", "sales.transactions", "labor.pct", "labor.target_pct"),
    "went_well": [_it("Net sales came to $2,000.", "sales.net"),
                  _it("Labor held at 22%.", "labor.pct")],
    "needs_attention": [_it("Comps ran $40.", "sales.comps")],
    "biggest_risk": None, "biggest_win": None, "biggest_financial_opportunity": None,
    "biggest_staffing_concern": None,
    "actions_tomorrow": [
        _act("Keep Tuesday's staffing pattern on the next schedule.", "Labor ran 22% against a 30% target.",
             "adjust_staffing", ["labor.pct", "labor.target_pct"], urgency="next_schedule"),
        _act("Go over the night's comps with the closing manager.", "Comps ran $40.", "adjust_pricing",
             ["sales.comps"]),
    ],
    "highest_priority_issue": _it("Comps ran $40.", "sales.comps"),
    "largest_opportunity": None, "largest_guest_experience": None, "largest_staffing": None,
}


# ── the restaurant and its night ────────────────────────────────────────────

def _restaurant(db):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@example.com",
                                       timezone="America/Chicago"), db_path=db)
    # The owner's own blended wage ($440 over 32h): labor dollars are costed
    # at a rate somebody entered, never Cavnar's assumed $26 (D1-5).
    update_restaurant(rid, {"open_times_json": json.dumps({d: "11:00am" for d in DAYS}),
                            "close_times_json": json.dumps({d: "11:00pm" for d in DAYS}),
                            "hourly_rate": 13.75, "hourly_rate_source": "set"}, db_path=db)
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (rid, DAY.isoformat(), "Tuesday", 22.0, 440.0, 2000.0, 32.0))
    conn.commit()
    conn.close()
    r = get_restaurant(rid, db_path=db)
    closeout.save(rid, {"went_well": "Patio full from 6 to 8.", "went_wrong": "Ice machine slow again.",
                        "shift_notes": "Jim closed."}, submitted_by="Jim", business_date=DAY.isoformat(),
                  db_path=db, restaurant=r)
    return r


def _app():
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role):
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 7, "restaurant_id": rid, "is_admin": 0, "role": role, "username": "u", "email": "u@x.com"})


def test_one_night_from_close_to_the_owner_and_manager_views(db, fake_pos, monkeypatch):
    r = _restaurant(db)
    model = _Client(REPLY)
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: model)

    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=RUN_AT, db_path=db)
    assert out["action"] == "final" and out["version"] == 1 and out["required_missing"] == []
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "final" and not rep["provisional"]
    assert rep["stages"]["closed_by"] == "pos"
    assert rep["stages"]["narrative"] == {"status": "written", "reason": None}

    # Every block is on the report, each with a status the contract knows.
    blocks = rep["facts"]["blocks"]
    assert list(blocks) == list(dsr.BLOCKS)
    assert {n: b["status"] for n, b in blocks.items()} == {
        "sales": dsr.READY, "labor": dsr.READY, "food": dsr.NOT_CONNECTED, "reviews": dsr.NOT_CONNECTED,
        "marketing": dsr.READY, "intel": dsr.READY, "closeout": dsr.READY}
    # What isn't connected says why, once, on the report.
    assert rep["facts"]["missing"] == [blocks["food"]["reason"], blocks["reviews"]["reason"]]
    sales = blocks["sales"]["metrics"]
    assert (sales["net"], sales["gross"], sales["transactions"], sales["comps"]) == (2000.0, 2150.0, 80.0, 40.0)
    # No history and no budget: None, never a zero that reads as a real figure.
    assert sales["last_week_net"] is None and sales["budget_net"] is None and sales["vs_last_year_pct"] is None
    # Labor is read against the DSR's own net sales, from the same night.
    assert blocks["labor"]["metrics"]["pct"] == 22.0 and blocks["labor"]["detail"]["pct_basis"] == "dsr_net"
    # The manager's words go through as they wrote them.
    fields = blocks["closeout"]["detail"]["fields"]
    assert (fields["went_wrong"], fields["shift_notes"]) == ("Ice machine slow again.", "Jim closed.")
    assert rep["facts"]["fiscal"]["label"] == "Week of 9/21/26"

    # The searchable history (Ask, the weekly grid) has the night.
    assert store.metric_series(r.id, "sales.net", DAY, DAY, db_path=db) == [(DAY.isoformat(), 2000.0)]
    assert store.find_days(r.id, "labor.pct", "<", 25, db_path=db) == [(DAY.isoformat(), 22.0)]
    assert store.metric_series(r.id, "sales.last_week_net", DAY, DAY, db_path=db) == []

    # One model call, over the saved facts, with the close-out fenced as untrusted.
    assert len(model.calls) == 1
    prompt = model.calls[0]["messages"][0]["content"]
    assert "sales.net = 2,000" in prompt and "labor.pct = 22%" in prompt
    fenced = prompt.split("MANAGER CLOSEOUT", 1)[1].split("<<<UNTRUSTED_GUEST_TEXT", 1)[1]
    assert "Ice machine slow again." in fenced.split("UNTRUSTED_GUEST_TEXT>>>", 1)[0]
    n = rep["narrative"]
    assert n["verification"]["dropped"] == [] and n["verification"]["kept"] == n["verification"]["checked"]
    assert [a["key"] for a in n["actions_tomorrow"]] == ["dsr_action:adjust_pricing:sales",
                                                         "dsr_action:adjust_staffing:labor"]

    # A later sweep leaves a finished night alone — no second version, no second call.
    again = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=RUN_AT.replace(hour=5), db_path=db)
    assert again["action"] == "none" and len(model.calls) == 1

    web = _app()
    # The owner (Erik, Jim) reads everything.
    _as(monkeypatch, r.id, "client")
    owner = web.get("/api/dsr/2026-09-22").get_json()
    assert owner["ok"] and owner["view"] == "owner" and owner["status"] == "final"
    assert owner["facts"]["blocks"]["sales"]["metrics"]["comps"] == 40.0
    assert owner["narrative"]["needs_attention"] == [{"text": "Comps ran $40.", "cites": ["sales.comps"]}]
    assert len(owner["narrative"]["actions_tomorrow"]) == 2

    # A manager reads the same night without the loss lines — and without
    # every sentence that cited one — but keeps everything else the summary said.
    _as(monkeypatch, r.id, "manager")
    mgr = web.get("/api/dsr/2026-09-22").get_json()
    assert mgr["ok"] and mgr["view"] == "manager"
    m = mgr["facts"]["blocks"]["sales"]["metrics"]
    assert "comps" not in m and "voids" not in m and "budget_net" not in m and m["net"] == 2000.0
    mn = mgr["narrative"]
    assert mn["executive_summary"] == REPLY["executive_summary"]
    assert mn["went_well"] == REPLY["went_well"]
    assert mn["needs_attention"] == [] and not mn.get("highest_priority_issue")
    assert [a["kind"] for a in mn["actions_tomorrow"]] == ["adjust_staffing"]
    assert "Comps" not in json.dumps(mgr)

    status = web.get("/api/dsr/2026-09-22/status").get_json()
    assert status["exists"] and status["status"] == "final"


def test_a_night_whose_sales_land_late_goes_out_provisional_then_final_as_version_2(db, fake_pos, monkeypatch):
    r = _restaurant(db)
    model = _Client(REPLY)
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: model)
    empty = dict(SALES_DAY, gross=0.0, net=0.0, transactions=0)
    tickets = {"day": empty}
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: (dict(tickets["day"]), "fakepos"))

    # Closed, but the tickets haven't reached the POS's cloud copy: wait.
    assert pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=RUN_AT, db_path=db)["action"] == "retry"
    # 4am Chicago, still nothing: it goes out provisional, with no summary
    # (there is nothing true to summarise without sales) and no model call.
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 9, 23, 9, 5), db_path=db)
    assert out["action"] == "provisional" and out["required_missing"] == ["sales"]
    v1 = store.get_report(r.id, DAY, db_path=db)
    assert v1["version"] == 1 and v1["provisional"] and v1["narrative"] is None and model.calls == []
    assert v1["facts"]["blocks"]["sales"]["metrics"] == {} or \
        all(v is None for v in v1["facts"]["blocks"]["sales"]["metrics"].values())

    # The tickets land; the hourly late-data check makes version 2.
    tickets["day"] = SALES_DAY
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 9, 23, 10, 10), db_path=db)
    assert out["action"] == "final" and out["version"] == 2
    v2 = store.get_report(r.id, DAY, db_path=db)
    assert v2["status"] == "final" and v2["facts"]["blocks"]["sales"]["metrics"]["net"] == 2000.0
    assert v2["stages"]["supersedes"] == 1 and len(model.calls) == 1
    assert v2["narrative"]["executive_summary"] == REPLY["executive_summary"]
    # Version 1 is kept as it went out, never edited in place.
    first = store.get_report(r.id, DAY, version=1, db_path=db)
    assert first["status"] == "provisional" and first["narrative"] is None

    _as(monkeypatch, r.id, "client")
    body = _app().get("/api/dsr/2026-09-22").get_json()
    assert body["version"] == 2 and [(v["version"], v["status"]) for v in body["versions"]] == [(1, "provisional"), (2, "final")]
