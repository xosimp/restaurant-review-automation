"""Employee audit fixes B7 (10/1/26): the pre-shift brief, staff AI, house
rules, docs, training and certifications.

  H16  a personal, cheaper brief: built once per restaurant and day
       (preshift.build_cached), the reader's role / hours / station first, a
       rush line from hourly shares or a game's pattern OFFSETS (never the
       playbook's dollar text), only on a day they work (AI-01 ph.1, AI-02,
       PERF-07, UX-36)
  V3   at most one small-model rewrite a day, validated as audience
       "staff", shown to staff only once a manager approves it (AI-01 ph.2)
  V4   tonight's focus item and the manager's line (AI-06)
  V11  a per-person language and translation of approved text, figure
       parity, cached (AI-08)
  V9   docs and certifications with an expiry reminder (MISS-15, AI-10)
  V10  answers only from the house rules, citing lines, refusing pay /
       people / discipline (AI-04)
  AI-05  "86'd last night: X" from matched ingredients only

Every model call is mocked: ai_utils.get_client is replaced for the whole
file, so nothing here can reach a provider.
"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from flask import Flask

import ai_utils
import auth
import models
from auth import create_session, create_staff_session, create_user, init_auth, set_user_role, upsert_membership
from models import Restaurant, create_restaurant

DAY = date(2026, 10, 2)          # a Friday
ITEMS = [
    {"kind": "volume", "text": "Expect a busy Friday — typically about 25% busier than an average day."},
    {"kind": "rush", "text": "Busiest around 6–8pm on a usual Friday."},
    {"kind": "watch", "text": "Watch service speed tonight: it has come up in 4 recent reviews, mostly on Fridays."},
]


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    import strategy_jobs
    for mod in (models, auth, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    import preshift
    preshift.invalidate()
    yield
    preshift.invalidate()


class _NoClient:
    """A client that fails the test if a real call is attempted."""
    @property
    def messages(self):
        raise AssertionError("a model call reached the client — every call in this file is mocked")


@pytest.fixture(autouse=True)
def _no_live_model(monkeypatch):
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: _NoClient())


def _msg(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           _cavnar_call_id=None)


class _Model:
    """create_with_retry's stand-in: records each call, answers in turn."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, client, **kw):
        self.calls.append(kw)
        assert "readiness" in kw
        r = self.replies.pop(0) if self.replies else ""
        if isinstance(r, Exception):
            raise r
        return _msg(r)


@pytest.fixture
def app():
    import staff_knowledge_routes as skr
    from staff_routes import staff_bp
    a = Flask(__name__, template_folder="../templates")
    a.register_blueprint(staff_bp)
    a.register_blueprint(skr.staff_knowledge_bp)
    a.register_blueprint(skr.knowledge_bp)
    a.register_blueprint(skr.knowledge_mobile_bp)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"))


def _staff(rid, username="priya", name="Priya Shah", job_role="Server"):
    uid = create_user(rid, username, f"{username}@x.test", "unused", generated=True)
    m = upsert_membership(uid, rid, "employee", employee_name=name)
    if job_role:
        c = models.get_conn()
        c.execute("UPDATE memberships SET job_role=? WHERE id=?", (job_role, m["id"]))
        c.commit()
        c.close()
    return uid, m["id"]


def _console(rid, username="erik", role=None):
    uid = create_user(rid, username, f"{username}@x.test", "unused", generated=True)
    if role:
        set_user_role(uid, role)
    return uid


def _as_staff(client, rid, uid):
    client.set_cookie("staff_session", create_staff_session(uid, rid))


def _as_console(client, uid):
    client.set_cookie("session_token", create_session(uid))


def _roster(monkeypatch, names=("Priya Shah", "Jake Moss", "Dana Lee", "Marco Ruiz")):
    import staff_roster
    monkeypatch.setattr(staff_roster, "roster_names_for_restaurant",
                        lambda rid, db_path=None: [(n, "Server") for n in names])


def _items(monkeypatch, items=ITEMS):
    import preshift
    calls = []

    def fake_build(rid, day=None, db_path=None):
        calls.append((rid, day))
        return {"day": (day or DAY).isoformat(), "weekday": (day or DAY).strftime("%A"), "items": list(items)}
    monkeypatch.setattr(preshift, "build", fake_build)
    monkeypatch.setattr(preshift, "business_day", lambda rid: DAY)
    return calls


def _shift(monkeypatch, shift=None, published=True):
    import staff_schedule
    monkeypatch.setattr(staff_schedule, "shifts_for_employee",
                        lambda rid, name, today=None: {"today": shift, "published": published})


def _ready(monkeypatch, decision="proceed"):
    import data_health
    monkeypatch.setattr(data_health, "readiness",
                        lambda *a, **k: {"decision": decision, "reason": "pos stale" if decision != "proceed" else "",
                                         "prompt_block": "", "data_state": {}})


# ── H16: built once per restaurant and day (PERF-07) ────────────────────────

def test_the_days_items_are_built_once_per_restaurant_and_day_within_the_ttl(monkeypatch):
    import preshift
    rid = _restaurant()
    calls = _items(monkeypatch)
    a = preshift.build_cached(rid, day=DAY, now=1000.0)
    b = preshift.build_cached(rid, day=DAY, now=1000.0 + preshift.BUILD_TTL_SECONDS - 1)
    assert a is b and len(calls) == 1, "25 staff opening the app run one build, not 25"
    preshift.build_cached(rid, day=DAY, now=1000.0 + preshift.BUILD_TTL_SECONDS + 1)
    assert len(calls) == 2, "rebuilt after the TTL"
    preshift.build_cached(rid, day=DAY + timedelta(days=1), now=1000.0)
    assert len(calls) == 3, "another business day is another build"
    preshift.invalidate(rid)
    preshift.build_cached(rid, day=DAY, now=1000.0)
    assert len(calls) == 4


def test_the_staff_route_reads_the_kept_build_not_a_fresh_one(client, monkeypatch):
    rid = _restaurant()
    calls = _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "16:00", "end": "23:00", "station": None})
    uids = [_staff(rid, f"s{i}", f"Person {i}")[0] for i in range(5)]
    for uid in uids:
        _as_staff(client, rid, uid)
        assert client.get("/staff/api/preshift").get_json()["ok"] is True
    assert len(calls) == 1


# ── H16: personal, and only on a working day (UX-36) ────────────────────────

def test_a_working_day_puts_the_readers_role_hours_and_station_first(client, monkeypatch):
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, {"role": "Line Cook", "start": "16:00", "end": "22:30", "station": "Grill"})
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    d = client.get("/staff/api/preshift").get_json()
    assert d["working"] is True
    assert d["items"][0] == {"kind": "you", "text": "You're on as Line Cook, 4pm–10:30pm."}
    assert d["items"][1] == {"kind": "station", "text": "Your station: Grill."}
    assert [i["text"] for i in d["items"][2:]] == [i["text"] for i in ITEMS]
    assert d["brief_text"] is None, "nothing approved: the deterministic lines only"


def test_a_double_names_both_legs(client, monkeypatch):
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "10:00", "end": "14:00",
                         "legs": [{"role": "Server", "start": "10:00", "end": "14:00"},
                                  {"role": "Server", "start": "17:00", "end": "22:00"}]})
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    assert client.get("/staff/api/preshift").get_json()["items"][0]["text"] == \
        "You're on as Server, 10am–2pm and 5pm–10pm."


def test_a_day_off_is_a_minimal_payload(client, monkeypatch):
    rid = _restaurant()
    calls = _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, None, published=True)
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    d = client.get("/staff/api/preshift").get_json()
    assert d["working"] is False and d["items"] == [] and d["brief_text"] is None and d["focus"] is None
    assert calls == [], "a day off builds nothing"


def test_with_no_published_schedule_everyone_keeps_the_card(client, monkeypatch):
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, None, published=False)
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    d = client.get("/staff/api/preshift").get_json()
    assert d["working"] is None and len(d["items"]) == len(ITEMS)


# ── AI-02: the rush window, from shares and offsets only ────────────────────

def test_the_rush_line_comes_from_the_hourly_shares(monkeypatch):
    import preshift
    import schedule_engine
    monkeypatch.setattr(schedule_engine, "_safe_hourly_profile",
                        lambda rid: {"Friday": {16: 0.08, 17: 0.12, 18: 0.21, 19: 0.19, 20: 0.1, 21: 0.05}})
    assert preshift._hourly_rush_line(1, "Friday") == "Busiest around 6–8pm on a usual Friday."
    monkeypatch.setattr(schedule_engine, "_safe_hourly_profile",
                        lambda rid: {"Friday": {11: 0.3, 12: 0.1, 18: 0.2}})
    assert preshift._hourly_rush_line(1, "Friday") == "Busiest around 11am–12pm on a usual Friday."
    assert preshift._hourly_rush_line(1, "Monday") is None, "no measured Mondays: nothing is said"


def test_the_game_rush_line_uses_offsets_never_the_playbooks_dollar_text(monkeypatch):
    """playbook.rush's `text` carries dollars ("$1,240 against $810"); the
    staff line is built from pattern_offset / pattern_span and kickoff only.
    Revert-check: reading rz["text"] puts a $ in the line."""
    import preshift
    from event_intel import engine, playbook, store
    monkeypatch.setattr(store, "event_by_id", lambda i, db_path=None: {"id": i, "category": "sports", "league": "NFL"})
    monkeypatch.setattr(playbook, "rush", lambda rid, e, db_path=None: {
        "pattern_offset": -1, "pattern_span": 1,
        "text": "On 9/14/26 the biggest jump came 6–7pm: $1,240 against $810."})
    monkeypatch.setattr(engine, "restaurant_clock", lambda rid, db_path=None: "America/Chicago")
    monkeypatch.setattr(engine, "local_kickoff", lambda e, tz=None: ("2026-10-02", "19:20"))
    line = preshift._game_rush_line(1, "events:7")
    assert line == "Expect the jump around 6–8pm — about an hour before kickoff."
    assert "$" not in line
    monkeypatch.setattr(playbook, "rush", lambda rid, e, db_path=None: {"pattern_offset": None, "text": "$5"})
    assert preshift._game_rush_line(1, "events:7") is None, "games that don't agree: nothing is said"


def test_the_rush_line_sits_after_the_volume_line(monkeypatch):
    import preshift
    import labor, schedule_engine, demand_signals
    import thresholds
    rid = _restaurant()
    monkeypatch.setattr(labor, "build_demand_forecast", lambda rid: {"days": [
        {"day": "Friday", "samples": thresholds.DEMAND_LEVEL_MIN_READINGS, "vs_average_pct": 30}]})
    monkeypatch.setattr(schedule_engine, "_safe_hourly_profile", lambda rid: {"Friday": {18: 0.2, 19: 0.1}})
    monkeypatch.setattr(demand_signals, "upcoming", lambda *a, **k: [])
    import weather
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    kinds = [i["kind"] for i in preshift.build(rid, day=DAY)["items"]]
    assert kinds[:2] == ["volume", "rush"]


# ── AI-05: 86'd last night, matched ingredients only ────────────────────────

def test_the_86_line_names_only_matched_ingredients_never_the_managers_words(monkeypatch):
    import preshift
    rid = _restaurant()
    c = models.get_conn()
    c.execute("INSERT INTO ingredients (restaurant_id, name, is_active) VALUES (?, 'Salmon Fillet', 1)", (rid,))
    c.execute("INSERT INTO ingredients (restaurant_id, name, is_active) VALUES (?, 'Short Rib', 1)", (rid,))
    c.execute("INSERT INTO close_outs (restaurant_id, business_date, eighty_sixed, went_wrong, callouts) "
              "VALUES (?,?,?,?,?)", (rid, (DAY - timedelta(days=1)).isoformat(),
                                     "salmon fillet, short rib because Jake dropped the tray, mystery fish",
                                     "Jake was rude to table 4", "Dana called out"))
    c.commit()
    c.close()
    line, names = preshift._eighty_sixed_line(rid, DAY)
    assert names == ["Salmon Fillet", "Short Rib"]
    assert line.startswith("86'd last night: Salmon Fillet, Short Rib.")
    assert "Jake" not in line and "Dana" not in line and "rude" not in line and "mystery" not in line
    assert preshift._eighty_sixed_line(rid, DAY + timedelta(days=3)) == (None, [])


# ── response_validation: the staff audience (S1) ────────────────────────────

def test_staff_surfaces_are_always_read_by_staff():
    import response_validation as rv
    assert rv.ValidationContext(surface="staff_brief").audience == "staff"
    assert rv.ValidationContext(surface="staff_answer", audience="owner").audience == "staff"
    with pytest.raises(ValueError):
        rv.ValidationContext(surface="ask", audience="staff")


@pytest.mark.parametrize("text,label", [
    ("We did $8k last Friday.", "money"),
    ("A few hundred dollars behind.", "money"),
    ("Your hourly rate goes up next month.", "pay"),
    ("The tip pool is split at close.", "pay"),
    ("Labor cost ran high.", "the owner's numbers"),
    ("Keep an eye on the margins.", "the owner's numbers"),
    ("Net sales were strong.", "the owner's numbers"),
    ("Her reliability score dropped.", "a rating of a person"),
    ("Late again means a write-up.", "discipline"),
    ("Jake is on the door.", "another person's name"),
    ("Dana's tables first.", "another person's name"),
])
def test_staff_unsafe_catches_the_must_never_reach_staff_list(text, label):
    import response_validation as rv
    why = rv.staff_unsafe(text, people_denied=["Jake Moss", "Dana Lee", "Priya Shah"], people_allowed=["Priya Shah"])
    assert why and why[0] == label


def test_staff_unsafe_lets_lineup_talk_and_the_readers_own_name_through():
    import response_validation as rv
    for ok in ("Expect a busy Friday with the rush around 6–8pm.", "Priya, you're on the patio.",
               "Fire the apps as soon as the ticket prints.", "Watch service speed tonight."):
        assert rv.staff_unsafe(ok, people_denied=["Jake Moss", "Priya Shah"], people_allowed=["Priya Shah"]) is None, ok


def test_a_staff_text_carries_only_the_sources_numbers():
    import response_validation as rv
    src = "Busiest around 6–8pm. Watch service speed: it came up in 4 recent reviews."
    ctx = rv.ValidationContext(surface="staff_brief", context_text=src, facts=rv.entity_facts({}, [src]))
    assert rv.validate("The rush is 6–8pm; service speed came up in 4 reviews.", ctx).verdict == "pass"
    v = rv.validate("Service speed came up in 9 reviews.", ctx)
    assert v.verdict == "refuse" and "F1" in v.codes


# ── V3: the one rewrite a day, approved before staff see it ─────────────────

def test_the_draft_is_one_mocked_call_a_day_validated_and_hidden_until_approved(client, monkeypatch):
    import staff_brief
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _ready(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "16:00", "end": "23:00"})
    model = _Model("Busy Friday tonight, about 25% busier than average, with the rush around 6–8pm.")
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    row = staff_brief.draft(rid, day=DAY)
    assert row["draft_status"] == "drafted" and row["draft_text"].startswith("Busy Friday")
    assert model.calls[0]["action"] == "staff_brief" and model.calls[0]["model"] == ai_utils.model_for("staff_brief")
    staff_brief.draft(rid, day=DAY)
    assert len(model.calls) == 1, "at most one model call per restaurant per day"

    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    assert client.get("/staff/api/preshift").get_json()["brief_text"] is None, "a draft is never shown to staff"
    owner = _console(rid)
    staff_brief.approve(rid, {"id": owner, "role": "client"}, day=DAY)
    assert client.get("/staff/api/preshift").get_json()["brief_text"].startswith("Busy Friday")


def test_a_draft_that_fails_the_staff_check_is_not_offered(monkeypatch):
    """Revert-check: without the staff audience the $ figure would be F1-only
    and a hybrid context could let it through."""
    import staff_brief
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _ready(monkeypatch)
    for bad in ("Last Friday did $8,400, so hustle tonight.", "Jake is on the door tonight with a busy Friday.",
                "Expect about 35% more guests tonight."):
        monkeypatch.setattr(ai_utils, "create_with_retry", _Model(bad))
        c = models.get_conn()
        c.execute("DELETE FROM staff_briefs")
        c.commit()
        c.close()
        row = staff_brief.draft(rid, day=DAY)
        assert row["draft_status"] == "refused" and row["draft_text"] is None, bad


def test_a_held_data_state_or_a_single_line_spends_no_call(monkeypatch):
    import staff_brief
    rid = _restaurant()
    _roster(monkeypatch)
    model = _Model("unused")
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    _items(monkeypatch)
    _ready(monkeypatch, "caveat")
    assert staff_brief.draft(rid, day=DAY)["draft_status"] == "held"
    _items(monkeypatch, ITEMS[:1])
    import preshift
    preshift.invalidate()
    assert staff_brief.draft(rid, day=DAY + timedelta(days=1))["draft_status"] == "no_items"
    assert model.calls == []
    # Held is not spent: once the data is current the day's call can still run.
    _items(monkeypatch)
    preshift.invalidate()
    _ready(monkeypatch)
    model.replies = ["Busy Friday with the rush around 6–8pm."]
    assert staff_brief.draft(rid, day=DAY)["draft_status"] == "drafted" and len(model.calls) == 1


def test_a_manager_edit_is_held_to_the_staff_rule(monkeypatch):
    import staff_brief
    rid = _restaurant()
    _roster(monkeypatch)
    user = {"id": _console(rid), "role": "client"}
    for bad in ("We did $8k last Friday, let's beat it.", "Jake has the door tonight.", "Keep labor cost down."):
        with pytest.raises(staff_brief.BriefError):
            staff_brief.approve(rid, user, day=DAY, text=bad)
    out = staff_brief.approve(rid, user, day=DAY, text="Big Friday. Rush around 6–8pm. Smile.")
    assert out["approved_text"] == "Big Friday. Rush around 6–8pm. Smile." and out["edited"] is True
    import change_log
    assert any(r["entity"] == "staff_brief" for r in change_log.history(rid, kinds=["setting"]))


# ── V4: the focus item ──────────────────────────────────────────────────────

def test_staff_see_the_focus_item_and_line_never_the_reason(client, monkeypatch):
    import menu_intelligence as mi
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "16:00", "end": "23:00"})
    monkeypatch.setattr(mi, "dish_scorecard", lambda rid, db_path=None: {"dishes": [
        {"name": "Fall Old Fashioned", "action": "promote", "total_contribution": 900}]})
    monkeypatch.setattr(mi, "dish_praise", lambda rid, db_path=None: [
        {"name": "Short Rib", "positive_reviews": 3, "negative_reviews": 0}])
    owner = _console(rid)
    _as_console(client, owner)
    d = client.get("/api/staff-brief").get_json()
    items = [s["item"] for s in d["brief"]["suggestions"]]
    assert items == ["Fall Old Fashioned", "Short Rib"]
    assert all("$" not in s["why"] for s in d["brief"]["suggestions"])
    r = client.post("/api/staff-brief/focus", json={"item": "Fall Old Fashioned", "line": "Offer it with the burger."})
    assert r.status_code == 200, r.get_json()
    assert client.post("/api/staff-brief/focus", json={"item": "Ribeye", "line": "It has our best margins."}).status_code == 400

    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    p = client.get("/staff/api/preshift").get_json()
    assert p["focus"] == {"item": "Fall Old Fashioned", "line": "Offer it with the burger."}
    assert "why" not in json.dumps(p) and "Earns well" not in json.dumps(p)


def test_a_manager_without_the_food_view_gets_no_margin_suggestions(monkeypatch):
    import menu_intelligence as mi
    import staff_brief
    monkeypatch.setattr(mi, "dish_scorecard", lambda rid, db_path=None: {"dishes": [
        {"name": "Fall Old Fashioned", "action": "promote"}]})
    monkeypatch.setattr(mi, "dish_praise", lambda rid, db_path=None: [])
    assert staff_brief.focus_suggestions(1, {"role": "manager"}) == []
    assert staff_brief.focus_suggestions(1, {"role": "client"})[0]["item"] == "Fall Old Fashioned"


def test_suggestions_skip_what_ran_out(monkeypatch):
    import menu_intelligence as mi
    import staff_brief
    monkeypatch.setattr(mi, "dish_scorecard", lambda rid, db_path=None: {"dishes": []})
    monkeypatch.setattr(mi, "dish_praise", lambda rid, db_path=None: [
        {"name": "Short Rib", "positive_reviews": 3, "negative_reviews": 0}])
    out = staff_brief.focus_suggestions(1, {"role": "client"},
                                        items=[{"kind": "eighty_sixed", "text": "86'd last night: Short Rib."}])
    assert out == []


# ── the owner routes: permissions and twins ─────────────────────────────────

def test_owner_routes_have_mobile_twins_and_refuse_the_wrong_logins(client, monkeypatch):
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    member = _console(rid, "teammate", role="member")
    _as_console(client, member)
    assert client.post("/api/staff-brief/approve", json={"text": "Big night."}).status_code == 403
    assert client.post("/api/house-rules", json={"body": "x"}).status_code == 403
    manager = _console(rid, "mgr", role="manager")
    token = create_session(manager)
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/mobile/api/staff-brief/approve", json={"text": "Big night tonight."}, headers=h).status_code == 200
    assert client.get("/mobile/api/staff-brief", headers=h).get_json()["brief"]["approved_text"] == "Big night tonight."
    assert client.post("/mobile/api/house-rules", json={"body": "x"}, headers=h).status_code == 403, \
        "the house rules are the owner's"
    uid, _mid = _staff(rid)
    client.set_cookie("session_token", create_staff_session(uid, rid))
    assert client.get("/api/staff-brief").status_code in (401, 403)
    assert client.post("/api/staff-brief/approve", json={"text": "x"}).status_code in (401, 403)


def test_a_write_names_today_or_tomorrow_only(client, monkeypatch):
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _as_console(client, _console(rid))
    assert client.post("/api/staff-brief/approve", json={"text": "Hi team.", "day": "2026-09-01"}).status_code == 400
    assert client.post("/api/staff-brief/approve", json={"text": "Hi team.", "day": "2026-10-03"}).status_code == 200


# ── V11: language and translation ───────────────────────────────────────────

def test_a_person_sets_their_own_language(client, monkeypatch):
    rid = _restaurant()
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    assert client.get("/staff/api/language").get_json()["language"] == "en"
    assert client.post("/staff/api/language", json={"language": "klingon"}).status_code == 400
    d = client.post("/staff/api/language", json={"language": "es"}).get_json()
    assert d["language"] == "es" and {"code": "es", "name": "Spanish"} in d["languages"]


def test_translation_is_cached_and_held_to_figure_parity(monkeypatch):
    import staff_knowledge as sk
    rid = _restaurant()
    _roster(monkeypatch)
    _uid, mid = _staff(rid)
    sk.set_language(mid, rid, "es")
    src = "Busy Friday: about 25% busier, rush around 6–8pm."
    model = _Model("Viernes con mucho trabajo: alrededor de 25% más, más movimiento entre 6–8pm.")
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    out = sk.translate_for(mid, src)
    assert out.startswith("Viernes") and model.calls[0]["action"] == "staff_translation"
    assert sk.translate_for(mid, src) == out and len(model.calls) == 1, "cached per text and language"
    # A translation that drops or changes a figure is not used, and the
    # failure is remembered (no model call on every page read).
    src2 = "Rush around 6–8pm, 4 reviews mention speed."
    model2 = _Model("Más movimiento entre 6–9pm, cuatro reseñas.")
    monkeypatch.setattr(ai_utils, "create_with_retry", model2)
    assert sk.translate_for(mid, src2) == src2
    assert sk.translate_for(mid, src2) == src2 and len(model2.calls) == 1
    # Dropping a figure passes every other check (its numbers are all the
    # source's) — only parity catches it.
    src4 = "Rush around 6–8pm; 4 reviews mention speed."
    monkeypatch.setattr(ai_utils, "create_with_retry", _Model("Más movimiento entre 6–8pm; reseñas sobre rapidez."))
    assert sk.translate_for(mid, src4) == src4
    # A translation carrying money is refused (S1) — English stands.
    src3 = "Big night: 25% busier."
    monkeypatch.setattr(ai_utils, "create_with_retry", _Model("Gran noche: 25% más, $25 extra."))
    assert sk.translate_for(mid, src3) == src3


def test_figure_parity():
    import staff_knowledge as sk
    assert sk.figure_parity("6–8pm, 25%", "25% entre 6–8pm")
    assert not sk.figure_parity("6–8pm, 25%", "entre seis y ocho, 25%")
    assert not sk.figure_parity("25%", "25% y 30%")


def test_staff_read_the_approved_brief_and_focus_line_in_their_language(client, monkeypatch):
    import staff_brief
    import staff_knowledge as sk
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "16:00", "end": "23:00"})
    uid, mid = _staff(rid)
    sk.set_language(mid, rid, "es")
    user = {"id": _console(rid), "role": "client"}
    staff_brief.approve(rid, user, day=DAY, text="Big Friday. Rush around 6–8pm.")
    staff_brief.set_focus(rid, user, "Short Rib", "Offer it first.", day=DAY)
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        _Model("Gran viernes. Más movimiento entre 6–8pm.", "Ofrécelo primero."))
    _as_staff(client, rid, uid)
    d = client.get("/staff/api/preshift").get_json()
    assert d["brief_text"] == "Gran viernes. Más movimiento entre 6–8pm." and d["translated"] is True
    assert d["focus"] == {"item": "Short Rib", "line": "Ofrécelo primero."}
    assert d["language"] == "es"


# ── no $ and no other person's name can reach a staff payload ──────────────

def test_nothing_from_the_never_list_reaches_a_staff_payload_by_any_path(client, monkeypatch):
    """Every way text reaches /staff/api/preshift — the model's draft, the
    manager's edit, the focus item and line, a translation — tried with a
    dollar figure and a teammate's name. The payload stays clean."""
    import staff_brief
    import staff_knowledge as sk
    rid = _restaurant()
    _items(monkeypatch)
    _roster(monkeypatch)
    _ready(monkeypatch)
    _shift(monkeypatch, {"role": "Server", "start": "16:00", "end": "23:00"})
    user = {"id": _console(rid), "role": "client"}
    monkeypatch.setattr(ai_utils, "create_with_retry", _Model("Jake did $900 in sales last Friday — beat him."))
    assert staff_brief.draft(rid, day=DAY)["draft_status"] == "refused"
    with pytest.raises(staff_brief.BriefError):
        staff_brief.approve(rid, user, day=DAY)                       # no draft to approve
    for text in ("Dana did $900 last Friday.", "Ask Marco about the patio."):
        with pytest.raises(staff_brief.BriefError):
            staff_brief.approve(rid, user, day=DAY, text=text)
    for item, line in (("Ribeye", "Margins are best on it."), ("Ribeye", "Jake sells the most."),
                       ("$12 burger", "")):
        with pytest.raises(staff_brief.BriefError):
            staff_brief.set_focus(rid, user, item, line, day=DAY)
    staff_brief.approve(rid, user, day=DAY, text="Big Friday. Rush around 6–8pm.")
    uid, mid = _staff(rid)
    sk.set_language(mid, rid, "es")
    monkeypatch.setattr(ai_utils, "create_with_retry", _Model("Gran viernes, Jake: $50 más. 6–8pm."))
    _as_staff(client, rid, uid)
    raw = json.dumps(client.get("/staff/api/preshift").get_json(), ensure_ascii=False)
    assert "$" not in raw
    for other in ("Jake", "Dana", "Marco"):
        assert other not in raw, other
    assert "Big Friday. Rush around 6–8pm." in raw, "the English stands when the translation fails"


# ── V9: docs and certifications ─────────────────────────────────────────────

def test_staff_see_the_house_rules_their_roles_docs_and_only_their_own_certs(client, monkeypatch):
    rid = _restaurant()
    _as_console(client, _console(rid))
    assert client.post("/api/house-rules", json={"body": "Phones stay in the locker.\nShift meal after close."}).status_code == 200
    assert client.post("/api/staff-docs", json={"kind": "menu_spec", "title": "Bar specs", "body": "Old fashioned: 2 oz rye.",
                                                "roles": ["Bartender"]}).status_code == 200
    assert client.post("/api/staff-docs", json={"kind": "allergens", "title": "Allergens", "body": "Ask the kitchen."}).status_code == 200
    assert client.post("/api/staff-docs", json={"kind": "nope", "title": "x", "body": "y"}).status_code == 400
    assert client.post("/api/staff-certs", json={"employee_name": "Priya Shah", "cert": "Food handler",
                                                 "expires_on": "2026-12-01"}).status_code == 200
    assert client.post("/api/staff-certs", json={"employee_name": "Jake Moss", "cert": "alcohol",
                                                 "expires_on": "2026-10-20"}).status_code == 200
    assert client.post("/api/staff-certs", json={"employee_name": "Jake Moss", "cert": "alcohol",
                                                 "expires_on": "10/20/26"}).status_code == 400
    uid, _mid = _staff(rid, job_role="Server")
    _as_staff(client, rid, uid)
    d = client.get("/staff/api/docs").get_json()
    assert d["house_rules"]["body"].startswith("Phones stay")
    assert [x["title"] for x in d["docs"]] == ["Allergens"], "the bar spec is the bartenders'"
    assert d["certifications"] == [{"cert": "food handler", "expires_on": "2026-12-01",
                                    "days_left": d["certifications"][0]["days_left"], "status": "current"}]
    assert "Jake" not in json.dumps(d)


def test_a_cert_reminder_goes_once_to_the_holder_and_the_owner_and_rearms_on_renewal(monkeypatch):
    import emails
    import people
    import staff_knowledge as sk
    rid = _restaurant()
    owner = _console(rid)
    told, mailed = [], []
    monkeypatch.setattr(people, "tell", lambda rid, name, title, lines, **k: told.append((name, lines[0])) or "push")
    monkeypatch.setattr(emails, "deliver", lambda **k: mailed.append(k) or SimpleNamespace(ok=True))
    user = {"id": owner, "role": "client"}
    sk.save_cert(rid, user, "Jake Moss", "alcohol", expires_on="2026-10-20")
    sk.save_cert(rid, user, "Dana Lee", "food handler", expires_on="2027-06-01")
    sk.save_cert(rid, user, "Priya Shah", "first aid")
    out = sk.remind_restaurant(rid, today=DAY)
    assert out == {"due": 1, "told": 1, "owner": True}
    assert told == [("Jake Moss", "Your alcohol certificate expires on 10/20/26.")]
    assert mailed[0]["payload"]["to"] == ["erik@x.test"] and mailed[0]["email_type"] == "cert_expiry"
    assert sk.remind_restaurant(rid, today=DAY + timedelta(days=1))["due"] == 0, "once per expiry date"
    sk.save_cert(rid, user, "Jake Moss", "alcohol", expires_on="2026-10-25")
    assert sk.remind_restaurant(rid, today=DAY + timedelta(days=2))["due"] == 1, "a renewal re-arms it"
    assert sk.expired_certs(rid, today=date(2026, 11, 1)) == {"jake moss": {"alcohol"}}


def test_the_cert_job_returns_the_standard_counts(monkeypatch, db_path):
    import scheduler
    import staff_knowledge as sk
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(sk, "remind_restaurant", lambda rid, today=None, db_path=None: {"due": 1, "told": 1, "owner": True})
    rid = _restaurant()
    out = sk.run_cert_reminders(db_path=db_path, restaurants=[models.get_restaurant(rid)])
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out) and out["sent"] >= 1


# ── V10: answers from the house rules ───────────────────────────────────────

def _rules(rid):
    import staff_knowledge as sk
    sk.save_doc(rid, {"id": None}, "house_rules", "House rules",
                "Phones stay in the locker during service.\nShift meal is after close, from the staff menu.\n"
                "Call the manager on duty at least 2 hours before your shift if you can't make it.")


def test_pay_people_and_discipline_are_refused_before_any_model_call(client, monkeypatch):
    rid = _restaurant()
    _roster(monkeypatch)
    _rules(rid)
    model = _Model("unused")
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    for q, why in (("When is payday?", "pay"), ("How are tips split?", "pay"),
                   ("Is Jake working tonight?", "other_people"), ("Will I get a write-up for being late?", "discipline")):
        d = client.post("/staff/api/ask", json={"question": q}).get_json()
        assert d["answered"] is False and d["reason"] == why and d["suggest_message"] is True, q
        assert d["answer"].startswith("Ask your manager")
    assert model.calls == []


def test_an_answer_cites_lines_from_the_store_not_the_model(client, monkeypatch):
    rid = _restaurant()
    _roster(monkeypatch)
    _rules(rid)
    model = _Model(json.dumps({"found": True, "answer": "Phones stay in the locker during service. [S1]",
                               "sources": ["S1", "S99"]}))
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    d = client.post("/staff/api/ask", json={"question": "Can I keep my phone on me?"}).get_json()
    assert d["answered"] is True and d["answer"] == "Phones stay in the locker during service."
    assert d["sources"] == [{"id": "S1", "source": "House rules", "kind": "house_rules",
                             "line": "Phones stay in the locker during service."}]
    # The first rung of the staff_answer policy (T1 since the AI orchestration,
    # 10/7/26 — it was the call site's own Sonnet); T2 only on escalation.
    import ai_workflows
    assert model.calls[0]["action"] == "staff_answer" and \
        model.calls[0]["model"] == ai_workflows.route_for(ai_workflows.POLICIES["staff_answer"], 0).model


@pytest.mark.parametrize("reply,reason", [
    ({"found": True, "answer": "Phones stay in the locker.", "sources": []}, "not_found"),
    ({"found": True, "answer": "Phones stay in the locker.", "sources": ["S42"]}, "not_found"),
    ({"found": False, "answer": "", "sources": []}, "not_found"),
    ({"found": True, "answer": "Call at least 4 hours before your shift.", "sources": ["S3"]}, "unchecked"),
    ({"found": True, "answer": "Ask Marco to hold your phone.", "sources": ["S1"]}, "unchecked"),
    ({"found": True, "answer": "Shift meal is after close and costs $5.", "sources": ["S2"]}, "unchecked"),
])
def test_an_uncited_or_unchecked_answer_says_ask_your_manager(client, monkeypatch, reply, reason):
    rid = _restaurant()
    _roster(monkeypatch)
    _rules(rid)
    # The same reply on both rungs: a first reading that fails escalates once
    # (AI orchestration, 10/7/26), and the reason is the last rung's — except
    # found: false, which is final on the first (re-audit #3).
    monkeypatch.setattr(ai_utils, "create_with_retry", _Model(json.dumps(reply), json.dumps(reply)))
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    d = client.post("/staff/api/ask", json={"question": "Phone rules?"}).get_json()
    assert d["answered"] is False and d["reason"] == reason and d["suggest_message"] is True


def test_no_house_rules_means_no_call_and_asking_is_rate_limited(client, monkeypatch):
    import staff_knowledge as sk
    rid = _restaurant()
    _roster(monkeypatch)
    model = _Model()
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    uid, _mid = _staff(rid)
    _as_staff(client, rid, uid)
    assert client.post("/staff/api/ask", json={"question": "Where do coats go?"}).get_json()["reason"] == "no_rules"
    assert model.calls == []
    codes = [client.post("/staff/api/ask", json={"question": "Where do coats go?"}).status_code
             for _ in range(sk.ASK_PER_MINUTE + 1)]
    assert 429 in codes


# ── the nudge: the one draft, and the manager's ask ─────────────────────────

def test_the_nudge_drafts_once_and_asks_the_manager_to_approve(monkeypatch, db_path):
    import issues, strategy_jobs, time_utils
    rid = _restaurant()
    models.update_restaurant(rid, {"preshift_nudge_hour": 16})
    c = models.get_conn()
    cid = c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?, 'GM', '+15555550100', 1)",
                    (rid,)).lastrowid
    c.commit()
    c.close()
    issues.set_routing(rid, "manager", cid)
    _items(monkeypatch)
    _roster(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 10, 2, 16, 10))
    model = _Model("Busy Friday with the rush around 6–8pm.")
    monkeypatch.setattr(ai_utils, "create_with_retry", model)
    sent = []
    monkeypatch.setattr("notify.send_sms", lambda to, msg, use_case="alert": sent.append(msg) or True)
    assert strategy_jobs.run_preshift_nudge(db_path=db_path)["sent"] == 1
    assert len(model.calls) == 1
    msg = sent[0]
    assert len(msg) <= 320 and "Approve the drafted brief" in msg and "/staff/r/" in msg \
        and "nav=account%2Fnotifications" in msg
