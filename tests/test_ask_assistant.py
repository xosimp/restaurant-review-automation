"""The Ask Cavnar opening, its memory, and the context it can now reach.

Seven things the assistant could not do before: open with a briefing,
suggest questions drawn from the owner's own numbers, read the Shift
Quality verdict, read the roster, read what has fired, say how old the
data is, remember anything between conversations, and know it is one of
several locations. Each is covered here at the layer that would actually
break — the route for the two the panel reads on open, the builder for
the five that only reach the model.
"""
import json

import pytest
from flask import Flask

import ask_cavnar
import ask_cavnar_tools as tools
import auth
import auth_routes
import client_api
import mobile_api
import models
import push
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn
from mobile_api import mobile_bp


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api, tools, ask_cavnar, push):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


@pytest.fixture(autouse=True)
def _auth_tables(db_path):
    init_auth(db_path=db_path)
    from push import init_push
    init_push(db_path=db_path)


@pytest.fixture
def client(db_path):
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Simple EJ's")
    kw.setdefault("owner_email", "erik@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _token(client, db_path, rid):
    create_user(rid, "erik", "erik@x.test", "correct-horse", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": "erik", "password": "correct-horse"})
    return {"Authorization": "Bearer " + resp.get_json()["token"]}


def _opening(client, db_path, rid, payload):
    """Drive the route with a known home_brief payload."""
    import home_brief
    headers = _token(client, db_path, rid)
    import mobile_api as m
    m.home_brief = None  # the route imports it itself; make sure that import is the patched one
    del m.home_brief
    orig = home_brief.build_home_brief
    home_brief.build_home_brief = lambda user: (payload, 200)
    try:
        return client.get("/mobile/api/ask-cavnar/opening", headers=headers).get_json()
    finally:
        home_brief.build_home_brief = orig


# ── 1 + 2. the opening: a briefing and questions drawn from real signals ──

def test_the_opening_carries_the_owners_own_questions_not_three_fixed_ones(client, db_path):
    rid = _restaurant(db_path)
    body = _opening(client, db_path, rid, {
        "attention": [], "wins": [],
        "ask_suggestions": ["Why did Saturday score 44?", "Where is labor leaking?"],
        "greeting_name": "Erik", "context": {"restaurant_name": "Simple EJ's"},
    })
    assert body["suggestions"] == ["Why did Saturday score 44?", "Where is labor leaking?"]
    assert body["greeting_name"] == "Erik"


def test_the_briefing_puts_the_worst_thing_first(client, db_path):
    rid = _restaurant(db_path)
    body = _opening(client, db_path, rid, {
        "attention": [
            {"severity": "watch", "title": "Inventory drifting", "detail": "d", "module": "inventory"},
            {"severity": "critical", "title": "One-star review unanswered", "detail": "d", "module": "reviews"},
            {"severity": "important", "title": "Labor over target", "detail": "d", "module": "labor"},
        ],
        "wins": [{"title": "Rating up to 4.6", "detail": "d", "module": "reviews"}],
        "ask_suggestions": ["q"],
    })
    assert [b["severity"] for b in body["briefing"]] == ["critical", "important", "watch", "good"]
    assert body["headline"]


def test_a_broken_home_brief_still_opens_the_panel(client, db_path):
    import home_brief
    rid = _restaurant(db_path)
    headers = _token(client, db_path, rid)
    orig = home_brief.build_home_brief

    def explode(user):
        raise RuntimeError("home brief is down")

    home_brief.build_home_brief = explode
    try:
        resp = client.get("/mobile/api/ask-cavnar/opening", headers=headers)
    finally:
        home_brief.build_home_brief = orig
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["suggestions"], "the panel must still have something to offer"


def test_the_opening_is_behind_a_login(client):
    assert client.get("/mobile/api/ask-cavnar/opening").status_code == 401


# ── 3. Shift Quality reaches the assistant ───────────────────────────────

def test_the_schedule_tool_carries_the_shift_quality_verdict(db_path):
    rid = _restaurant(db_path)
    quality = {"checked": True, "score": 71, "band": "fair",
               "confidence": {"level": "medium", "reasons": ["4 of 19 rated"]},
               "weaknesses": ["Saturday night has no closer"],
               "strengths": [], "recommendations": [],
               "shifts": [{"day": "Saturday", "daypart": "dinner", "score": 44,
                           "scored": True, "meets_profile": False,
                           "profile": {"label": "peak"}, "weaknesses": ["no closer"]},
                          {"day": "Monday", "daypart": "lunch", "scored": False}]}
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, "
                 "hours_scheduled, hours_budget, schedule_csv, quality_json) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (rid, "2026-09-14", "2026-09-20", 320, 330, "", json.dumps(quality)))
    conn.commit()
    conn.close()
    out = tools._read_schedule(rid)
    assert out["quality"]["score"] == 71
    assert out["quality"]["confidence"] == "medium"
    assert [s["day"] for s in out["quality"]["shifts"]] == ["Saturday"], \
        "an unscored shift carries no verdict and must not be reported as one"


def test_a_schedule_with_no_evaluation_reports_no_quality(db_path):
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, "
                 "hours_scheduled, hours_budget, schedule_csv) VALUES (?,?,?,?,?,?)",
                 (rid, "2026-09-14", "2026-09-20", 320, 330, ""))
    conn.commit()
    conn.close()
    assert "quality" not in tools._read_schedule(rid)


def test_the_team_tool_says_so_rather_than_inventing_a_roster(db_path):
    rid = _restaurant(db_path)
    out = tools._read_team(rid)
    assert out["rated"] is False and out["note"]
    assert "team" not in out


# ── 4. alerts ────────────────────────────────────────────────────────────

def _alert(db_path, rid, alert_type="new_negative_review", review_id=None, days_ago=1):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_log (restaurant_id, review_id, alert_type, fired_at) "
                 "VALUES (?,?,?, datetime('now', ?))",
                 (rid, review_id, alert_type, "-%d days" % days_ago))
    conn.commit()
    conn.close()


def test_an_answered_alert_stops_counting_as_outstanding(db_path):
    from models import Review, save_reviews
    rid = _restaurant(db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="r1",
                         author="Ann", rating=1, text="Cold.")], db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()
    conn.execute("UPDATE reviews SET response_status='posted' WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    _alert(db_path, rid, review_id=row["id"])
    _alert(db_path, rid, alert_type="labor_over_target")
    out = tools._read_alerts(rid)
    assert len(out["alerts"]) == 2
    assert out["outstanding"] == 1


def test_an_alert_older_than_the_window_is_not_reported(db_path):
    rid = _restaurant(db_path)
    _alert(db_path, rid, days_ago=40)
    assert tools._read_alerts(rid, days=7)["alerts"] == []
    assert tools._read_alerts(rid, days=60)["alerts"]


def test_the_snapshot_says_plainly_when_nothing_has_fired(db_path):
    rid = _restaurant(db_path)
    assert "Nothing has fired" in ask_cavnar._alerts_context(rid)


def test_the_snapshot_lists_what_still_needs_the_owner(db_path):
    rid = _restaurant(db_path)
    _alert(db_path, rid, alert_type="labor_over_target")
    assert "Still needing action" in ask_cavnar._alerts_context(rid)


# ── 5. how old the data is ───────────────────────────────────────────────

def test_stale_data_is_labelled_as_stale():
    from datetime import date, timedelta
    today = date.today()
    assert "through today" in ask_cavnar._staleness(today.isoformat())
    assert "yesterday" in ask_cavnar._staleness((today - timedelta(days=1)).isoformat())
    assert "3 days ago" in ask_cavnar._staleness((today - timedelta(days=3)).isoformat())
    assert "not current" in ask_cavnar._staleness((today - timedelta(days=400)).isoformat())


def test_an_unparsable_date_says_nothing_rather_than_guessing():
    assert ask_cavnar._staleness("") == ""
    assert ask_cavnar._staleness(None) == ""
    assert ask_cavnar._staleness("last tuesday") == ""


def test_the_recent_days_lines_carry_the_last_days_with_sales(db_path):
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    for day, dow, sales in (("2026-09-08", "Monday", 4100), ("2026-09-09", "Tuesday", 5200)):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, "
                     "sales, labor_pct, total_hours) VALUES (?,?,?,?,?,?)",
                     (rid, day, dow, sales, 29.4, 112))
    conn.commit()
    conn.close()
    lines = ask_cavnar._recent_days_lines(rid)
    assert lines and "Most recent days" in lines[0]
    assert any("Tuesday 2026-09-09" in ln for ln in lines)
    assert lines[0] != "- Most recent days with recorded sales:", \
        "the heading has to say how old the newest day is"


def test_no_recorded_sales_produces_no_lines_at_all(db_path):
    rid = _restaurant(db_path)
    assert ask_cavnar._recent_days_lines(rid) == []


# ── 6. memory ────────────────────────────────────────────────────────────

def test_a_fact_survives_the_conversation_it_was_said_in(db_path):
    rid = _restaurant(db_path)
    tools._remember(rid, "Wants labor under 26% by December")
    facts = [f["fact"] for f in models.get_ask_memory(rid, db_path=db_path)]
    assert "Wants labor under 26% by December" in facts


def test_saying_the_same_thing_twice_does_not_store_it_twice(db_path):
    rid = _restaurant(db_path)
    tools._remember(rid, "Closes Mondays")
    tools._remember(rid, "Closes Mondays")
    assert len(models.get_ask_memory(rid, db_path=db_path)) == 1


def test_memory_does_not_grow_without_bound(db_path):
    """Counted in the table, not through get_ask_memory — the reader applies
    its own LIMIT, so reading it back cannot tell a trimmed table from one
    that has been growing for a year."""
    rid = _restaurant(db_path)
    for i in range(models.ASK_MEMORY_LIMIT + 5):
        models.remember_ask_fact(rid, "fact number %d" % i, db_path=db_path)
    conn = get_conn(db_path)
    stored = conn.execute("SELECT COUNT(*) AS n FROM ask_memory WHERE restaurant_id=?",
                          (rid,)).fetchone()["n"]
    conn.close()
    assert stored == models.ASK_MEMORY_LIMIT
    assert len(models.get_ask_memory(rid, db_path=db_path)) == models.ASK_MEMORY_LIMIT


def test_one_owners_memory_is_not_another_owners(db_path):
    rid_a = _restaurant(db_path)
    rid_b = _restaurant(db_path, name="Other Place", owner_email="other@x.test")
    models.remember_ask_fact(rid_a, "Erik hates surprises", db_path=db_path)
    assert models.get_ask_memory(rid_b, db_path=db_path) == []


def test_an_empty_fact_is_refused(db_path):
    rid = _restaurant(db_path)
    assert "error" in tools._remember(rid, "   ")


def test_a_forgotten_fact_is_gone(db_path):
    rid = _restaurant(db_path)
    models.remember_ask_fact(rid, "Closes Mondays", db_path=db_path)
    assert models.forget_ask_fact(rid, "Closes Mondays", db_path=db_path) is True
    assert models.get_ask_memory(rid, db_path=db_path) == []


def test_the_snapshot_marks_memory_as_told_not_measured(db_path):
    rid = _restaurant(db_path)
    models.remember_ask_fact(rid, "Wants labor under 26%", db_path=db_path)
    section = ask_cavnar._memory_context(rid)
    assert "Wants labor under 26%" in section
    assert "never present one as a fact" in section


def test_no_memory_means_no_section(db_path):
    rid = _restaurant(db_path)
    assert ask_cavnar._memory_context(rid) == ""


# ── 7. the assistant knows it is one of several locations ────────────────

def test_sibling_locations_are_named(db_path):
    rid_a = _restaurant(db_path, location_group="ejs", location_name="Simple EJ's — Downtown")
    _restaurant(db_path, name="Simple EJ's North", location_group="ejs",
                location_name="Simple EJ's — North")
    restaurant = models.get_restaurant(rid_a, db_path=db_path)
    assert ask_cavnar._sibling_locations(restaurant) == ["Simple EJ's — North"]


def test_another_owners_location_is_never_a_sibling(db_path):
    rid_a = _restaurant(db_path, location_group="ejs", location_name="Downtown")
    _restaurant(db_path, name="Someone Else", owner_email="rival@x.test",
                location_group="ejs", location_name="Rival Location")
    restaurant = models.get_restaurant(rid_a, db_path=db_path)
    assert ask_cavnar._sibling_locations(restaurant) == []


def test_a_single_location_has_no_siblings(db_path):
    rid = _restaurant(db_path)
    restaurant = models.get_restaurant(rid, db_path=db_path)
    assert ask_cavnar._sibling_locations(restaurant) == []


# ── the snapshot wires the always-on sections in ─────────────────────────

def test_alerts_and_memory_reach_the_snapshot_without_a_module(db_path):
    rid = _restaurant(db_path)
    models.set_service_tier(rid, "starter_marketing", db_path=db_path)
    models.remember_ask_fact(rid, "Wants labor under 26%", db_path=db_path)
    _alert(db_path, rid, alert_type="labor_over_target")
    snapshot = ask_cavnar.build_context(models.get_restaurant(rid, db_path=db_path))
    assert "WHAT THIS OWNER HAS TOLD YOU BEFORE" in snapshot
    assert "ALERTS" in snapshot


# ── every new tool is actually callable by the model ─────────────────────

@pytest.mark.parametrize("name", ["read_team", "read_alerts", "remember"])
def test_the_new_tools_are_registered_and_dispatchable(name):
    entry = next((t for t in tools.TOOLS if t["spec"]["name"] == name), None)
    assert entry, "%s is not offered to the model" % name
    assert callable(entry["fn"]), "%s is offered but cannot be called" % name
    # All three execute rather than propose. "write" in this registry means an
    # effect outside the building — an email sent, a public reply posted. A
    # note the owner can ask to be forgotten is not one of those, so making
    # `remember` a proposal would mean confirming a dialog to be listened to.
    assert entry["kind"] == "read"


# ── both surfaces read the opening rather than shipping a fixed list ──────
#
# Asserted against the source, not a rendered payload: the failure these
# guard against is someone re-hardcoding the questions, which a fixture
# test on one response would never see.

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_web_panel_fetches_its_opening():
    js = _source("templates", "dashboard.html")
    assert "/api/ask-cavnar/opening" in js
    assert "o.suggestions" in js, "the web panel must prefer the server's questions"
    assert "o.briefing" in js, "the web panel must render the briefing"


def test_the_ios_panel_fetches_its_opening():
    swift = _source("ios", "CavnarAI", "CavnarAI", "Features", "AskCavnar",
                    "AskCavnarViewModel.swift")
    view = _source("ios", "CavnarAI", "CavnarAI", "Features", "AskCavnar",
                   "AskCavnarView.swift")
    assert "/mobile/api/ask-cavnar/opening" in swift
    assert "viewModel.opening" in view, "the iOS panel must render what the server sent"
    assert "activeSuggestions" in view


def test_the_web_route_is_the_same_endpoint_the_app_uses(db_path, monkeypatch):
    """The web's /api/ask-cavnar/opening delegates to the mobile handler, so
    the two surfaces can never drift into two different briefings."""
    import client_api
    src = _source("client_api.py")
    block = src[src.index('@client_bp.route("/api/ask-cavnar/opening")'):][:400]
    assert "_m(" in block, "the web route must delegate, not reimplement"


# ── what the re-audit found ──────────────────────────────────────────────

def test_the_owner_can_have_a_note_dropped(db_path):
    """Memory that can only be written to is memory the owner cannot correct.
    Found by the re-audit: nothing on either surface could delete one."""
    rid = _restaurant(db_path)
    models.remember_ask_fact(rid, "Wants labor under 26% by December", db_path=db_path)
    out = tools._forget(rid, "Wants labor under 26% by December")
    assert out["forgotten"]
    assert models.get_ask_memory(rid, db_path=db_path) == []


def test_a_note_can_be_dropped_without_quoting_it_word_for_word(db_path):
    rid = _restaurant(db_path)
    models.remember_ask_fact(rid, "Wants labor under 26% by December", db_path=db_path)
    assert tools._forget(rid, "labor under 26%")["forgotten"] == \
        "Wants labor under 26% by December"


def test_dropping_a_note_that_was_never_there_says_what_is_there(db_path):
    rid = _restaurant(db_path)
    models.remember_ask_fact(rid, "Closes Mondays", db_path=db_path)
    out = tools._forget(rid, "hires only veterans")
    assert "error" in out and out["remembered"] == ["Closes Mondays"]
    assert len(models.get_ask_memory(rid, db_path=db_path)) == 1


def test_forget_cannot_reach_another_restaurants_notes(db_path):
    rid_a = _restaurant(db_path)
    rid_b = _restaurant(db_path, name="Other Place", owner_email="other@x.test")
    models.remember_ask_fact(rid_a, "Closes Mondays", db_path=db_path)
    assert "error" in tools._forget(rid_b, "Closes Mondays")
    assert len(models.get_ask_memory(rid_a, db_path=db_path)) == 1


def test_a_window_the_model_phrased_badly_still_reads_alerts(db_path):
    """Found by the re-audit: days="abc" raised, and the caught error read to
    the owner as though nothing had fired."""
    rid = _restaurant(db_path)
    _alert(db_path, rid, alert_type="labor_over_target", days_ago=0)
    for bad in ("abc", None, "", 0, -5, 3.7, "7", 10000):
        out = tools._read_alerts(rid, days=bad)
        assert out["alerts"], "days=%r lost a real alert" % bad


def test_the_briefing_is_not_held_for_the_life_of_the_session():
    """Found by the re-audit: both surfaces cached the opening once, so an
    owner who acted on an item was warned about it again on their return."""
    web = _source("templates", "dashboard.html")
    assert "ASK_OPENING_TTL_MS" in web
    assert "_askOpeningAt" in web
    swift = _source("ios", "CavnarAI", "CavnarAI", "Features", "AskCavnar",
                    "AskCavnarViewModel.swift")
    assert "openingTTL" in swift and "openingLoadedAt" in swift
