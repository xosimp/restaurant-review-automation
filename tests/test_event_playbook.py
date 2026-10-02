"""event_intel.playbook — Event Intelligence phase 2 (owner, 10/1/26: "Tomorrow
is a Sunday Night Bears home game… add one bartender, one server after 4 PM").

From a game on the calendar to what to do about it, read only from this
restaurant's own nights: staffing by role against a usual same weekday, the
rush around kickoff, tonight's game against the last one like it, and the
morning brief's alert three days out. A plan is said only where games like
it were measured; below that, what the last one did is said as a fact.
"""
import json
import sys
from datetime import date, timedelta

import pytest

import models
import event_memory
from event_intel import engine, playbook, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

ST_CHARLES = (41.9142, -88.3087)
SAINTS = "2026-11-22"            # Bears vs Saints, home, 12:00, regular season (a Sunday)
GAMES = ("2026-09-20", "2026-10-04")   # the two home noon games before it


@pytest.fixture
def db(db_path, monkeypatch, tmp_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    # Nothing here reaches the National Weather Service.
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    bears_only(db_path, monkeypatch, tmp_path / "seasons")
    return db_path


def bears_only(db_path, monkeypatch, tmp_dir):
    """These tests are about one series: the Bears. The other bundled seasons
    (phase 4) are taken out of the catalog and the bundled folder."""
    import os
    import shutil
    from event_intel import store as _st
    keep = "nfl-chicago-bears-2026.json"
    os.makedirs(str(tmp_dir), exist_ok=True)
    shutil.copy(os.path.join(_st.SEASONS_DIR, keep), os.path.join(str(tmp_dir), keep))
    monkeypatch.setattr(_st, "SEASONS_DIR", str(tmp_dir))
    c = models.get_conn(db_path)
    try:
        c.execute("DELETE FROM catalog_events WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_follows WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_series WHERE slug != 'nfl-chicago-bears'")
        c.commit()
    finally:
        c.close()


def _restaurant(db, name="EJ Co", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, dict({"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, **kw), db_path=db)
    r = get_restaurant(rid, db_path=db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 11, 20), db_path=db)
    return r


def _outcome(db, rid, day, lift, net=11000.0, base=8000.0, heads=None):
    conn = models.get_conn(db)
    try:
        conn.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, net, baseline, "
                     "lift_pct, covers, labor_pct, headcount, headcount_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, day, date.fromisoformat(day).strftime("%A"), "event", "bears soldier field", net, base,
                      lift, None, 27.0, sum((heads or {}).values()) or None, json.dumps(heads) if heads else None))
        conn.commit()
    finally:
        conn.close()


def _staff(db, rid, day, roles, start="15:50"):
    """roles {role: people}: one punch each, the PM start for every role."""
    conn = models.get_conn(db)
    try:
        for role, n in roles.items():
            for i in range(n):
                who = f"{role} {i}"
                conn.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                             "shift_start, shift_end, actual_hours, source) VALUES (?,?,?,?,?,?,?,?,?)",
                             (rid, day, who, who.lower(), role, start, "23:00", 7.0, "rpower"))
        conn.commit()
    finally:
        conn.close()


def _tickets(db, rid, day, by_hour):
    conn = models.get_conn(db)
    try:
        for h, net in by_hour.items():
            conn.execute("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, "
                         "net_sales) VALUES (?,?,?,?,?,?)",
                         (rid, "rpower", f"{day}-{h}", day, f"{day}T{h:02d}:10:00", net))
        conn.commit()
    finally:
        conn.close()


def _usual_sundays(game):
    g = date.fromisoformat(game)
    return [(g - timedelta(weeks=k)).isoformat() for k in range(1, 9)]


def _world(db, rid, lifts=(20.0, 30.0), game_bartenders=4):
    """Two measured home noon games; every ordinary Sunday before them ran
    3 PM bartenders, 8 PM servers, 6 in the kitchen."""
    for day, lift in zip(GAMES, lifts):
        if lift is not None:
            _outcome(db, rid, day, lift, heads={"Bartender PM": game_bartenders, "Server PM": 8, "Kitchen": 6})
        _staff(db, rid, day, {"Bartender PM": game_bartenders, "Server PM": 8, "Kitchen": 6})
        _tickets(db, rid, day, {10: 300, 11: 1900, 12: 900, 17: 700})
    usual = sorted({d for g in GAMES for d in _usual_sundays(g)} - set(GAMES) - {"2026-09-13"})
    for d in usual:
        _staff(db, rid, d, {"Bartender PM": 3, "Server PM": 8, "Kitchen": 6})
        _tickets(db, rid, d, {10: 300, 11: 500, 12: 600, 17: 700})


def _saints(db):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    return [e for e in store.events_for([s["id"]], SAINTS, SAINTS, db_path=db)][0]


def test_a_staffing_plan_needs_measured_games_that_all_ran_the_role_above_usual(db):
    r = _restaurant(db)
    _world(db, r.id)
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["n"] == 2 and st["usual"]["Bartender PM"] == 3
    plan = st["recommend"]
    assert [(p["role"], p["delta"], p["from"]) for p in plan] == [("Bartender PM", 1, "3:50pm")]
    assert st["text"] == ("Staff above a usual Sunday: 1 more Bartender PM from about 3:50pm. On your last 2 home "
                          "games you ran 4 Bartender PM against a usual Sunday's 3.")
    # the lift those games ran is data, never a second figure in the text (re-audit 2 R2-03)
    assert st["lift_pct"] == 25.0
    # roles that matched a usual Sunday are not said
    assert {d["role"] for d in st["deltas"]} == {"Bartender PM"}


def test_one_game_is_said_as_what_was_staffed_never_as_a_plan(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(20.0, None))
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["n"] == 1 and st["recommend"] == []
    assert st["text"] == ("On 9/20/26 you ran 4 Bartender PM against a usual Sunday's 3 — what was staffed, not "
                          "yet a pattern to plan on.")


def test_games_that_barely_moved_sales_never_become_a_plan(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(4.0, 6.0))
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["recommend"] == [] and "not yet a pattern" in st["text"]


def test_no_punches_on_file_no_staffing_read(db):
    r = _restaurant(db)
    _outcome(db, r.id, GAMES[0], 20.0)
    _outcome(db, r.id, GAMES[1], 30.0)
    assert playbook.staffing(r.id, _saints(db), db_path=db) is None


def test_prime_time_and_preseason_games_are_never_staffed_from_day_games(db):
    r = _restaurant(db)
    _world(db, r.id)
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    pats = store.events_for([s["id"]], "2026-10-22", "2026-10-22", db_path=db)[0]   # home, prime time
    assert playbook.staffing(r.id, pats, db_path=db) is None


def test_the_rush_is_read_around_kickoff_and_a_pattern_needs_two_games_that_agree(db):
    r = _restaurant(db)
    _world(db, r.id)
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert ru["n"] == 2 and ru["pattern_offset"] == -1
    assert [(g["peak_hour"], g["game"], g["usual"]) for g in ru["games"]] == [(11, 1900.0, 500.0)] * 2
    assert ru["text"] == "On your last 2 home games the biggest jump over a usual night came the hour before kickoff."


def test_one_games_rush_is_told_as_that_night(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(20.0, None))
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert ru["pattern_offset"] is None
    assert ru["text"] == ("On 9/20/26 the biggest jump over a usual Sunday came 11am–12pm, the hour before kickoff "
                          "(12pm): $1,900 against $500.")


def test_the_brief_names_the_game_three_days_out_with_the_plan_and_a_campaign(db):
    r = _restaurant(db)
    _world(db, r.id)
    line = playbook.alert(r.id, date(2026, 11, 20), marketing=True, db_path=db)
    assert line["key"].startswith("event_ahead:") and line["tone"] == "action" and line["claim_kind"] == "inferred"
    assert line["text"].startswith("Sunday 11/22/26: Bears vs New Orleans Saints · 12pm")
    assert "Bears home games have run +25% against a usual same weekday here" in line["text"]
    assert "Staff above a usual Sunday: 1 more Bartender PM from about 3:50pm." in line["text"]
    assert "Expect the jump around 11am–12pm (the hour before kickoff, as on your last 2)." in line["text"]
    assert line["action"]["label"] == "Draft a game-day campaign"
    assert line["action"]["nav"].startswith("marketing/campaigns?goal=Bring+guests+in+to+watch+Bears+vs+New+Orleans")
    # four days out is not yet
    assert playbook.alert(r.id, date(2026, 11, 18), db_path=db) is None


def test_an_unmeasured_game_is_said_as_the_last_one_never_as_a_lift(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(20.0, None))
    line = playbook.alert(r.id, date(2026, 11, 21), db_path=db)
    assert line["text"].startswith("Tomorrow: Bears vs New Orleans Saints") and line["tone"] == "neutral"
    assert ("No pattern measured yet: your last home game, Bears vs Minnesota Vikings · Sun 9/20/26 · 12pm"
            in line["text"]) and "$11,000, +20% against a usual Sunday — one night, not enough to plan on." in line["text"]
    assert "Staff above" not in line["text"] and "action" not in line


def test_a_viewer_without_labor_hears_the_game_but_not_the_dollars_or_staffing(db, monkeypatch):
    # The texting time is dropped once it has passed there: the clock is
    # pinned to 11/20/26, never the real one (re-audit 2 RX-08).
    import time_utils
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: noon.replace(tzinfo=None) if naive else noon)
    r = _restaurant(db)
    _world(db, r.id)
    line = playbook.alert(r.id, date(2026, 11, 20), sees_sales=False, sees_labor=False, marketing=True, db_path=db)
    assert line["text"] == ("Sunday 11/22/26: Bears vs New Orleans Saints · 12pm · FOX. Text your guests Sunday around "
                            "9am, 3 hours before kickoff (a starting rule, not yet measured here).")
    assert "$" not in line["text"] and "%" not in line["text"] and "Staff" not in line["text"]
    assert line["action"]["label"] == "Draft a game-day campaign"


def test_the_morning_brief_carries_the_game_line(db, monkeypatch):
    import morning_brief
    r = _restaurant(db, module_marketing=1)
    _world(db, r.id)
    brief = morning_brief.build(r.id, today=date(2026, 11, 20), db_path=db)
    game = [l for l in brief["lines"] if str(l.get("key")).startswith("event_ahead:")]
    assert game and "Bears vs New Orleans Saints" in game[0]["text"]
    assert "the game's staffing plan (an inference)" in morning_brief.footer_source(brief["lines"])


def test_tonights_game_is_set_against_the_last_one_of_the_same_side(db, monkeypatch):
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0, heads={"Bartender PM": 4, "Server PM": 8})
    monkeypatch.setattr(playbook, "usual_net", lambda rid, day, db_path=None: {"median": 8000.0, "n": 6})
    g = playbook.game_night(r.id, "2026-10-04", net=12000.0, guests=410, labor_pct=24.5, db_path=db)
    assert g["side"] == "home" and g["weekday"] == "Sunday" and g["tonight"]["lift_pct"] == 50.0
    assert g["last"]["date"] == "2026-09-28" and g["last"]["net"] == 11031.0 and g["last"]["headcount"] == 12
    assert g["text"] == ("Tonight's game sold $12,000, +50% against a usual Sunday ($8,000); your last home game, "
                         "Bears vs Philadelphia Eagles · Mon 9/28/26 · 7:15pm (prime time) · ESPN/ABC, sold $11,031, "
                         "+106% against a usual Monday.")
    assert playbook.game_night(r.id, "2026-10-05", net=5000.0, db_path=db) is None      # no game that night


def test_the_report_carries_the_game_and_its_labor_half_is_the_labor_views(db, monkeypatch):
    import dsr
    from dsr import access, block_intel, narrative
    r = _restaurant(db)
    _outcome(db, r.id, "2026-09-28", 106.1, net=11031.0, base=5351.0)
    monkeypatch.setattr(playbook, "usual_net", lambda rid, day, db_path=None: {"median": 8000.0, "n": 6})
    ctx = dsr.Context(r, date(2026, 10, 4), db_path=db)
    ctx.blocks = {"sales": {"status": dsr.READY, "metrics": {"net": 12000.0, "guests": 410}},
                  "labor": {"status": dsr.READY, "metrics": {"pct": 24.5}}}
    game = block_intel._game(ctx)
    assert game["tonight"]["net"] == 12000.0 and game["tonight"]["labor_pct"] == 24.5
    # the narrative never reads it: one narrative serves every view
    assert narrative._private("intel", "game")
    facts = {"blocks": {"intel": {"status": dsr.READY, "metrics": {}, "detail": {"game": game}}}}
    monkeypatch.setattr(access, "_sees", lambda user, perm: perm != "labor.view")
    out, _hidden = access.redact(facts, {"role": "manager"})
    g = out["blocks"]["intel"]["detail"]["game"]
    assert "labor_pct" not in g["tonight"] and "headcount" not in g["tonight"] and g["tonight"]["net"] == 12000.0


def test_the_day_after_says_who_worked_games_like_it_for_the_labor_view_only(db):
    from dsr import access, tomorrow
    r = _restaurant(db)
    _world(db, r.id)
    snap = tomorrow.build(r, date(2026, 11, 21), facts={}, db_path=db)
    st = [i for i in snap["items"] if i["kind"] == "game_staffing"]
    assert st and st[0]["text"].startswith("Staff above a usual Sunday: 1 more Bartender PM") and st[0]["tone"] == "warn"
    t = access.tomorrow_for({"tomorrow": snap}, {"role": "manager"}, access.view_for({"role": "manager"}),
                            withheld=["labor"])
    assert not [i for i in t["items"] if i["kind"] == "game_staffing"]


def test_ask_reads_the_staffing_and_the_rush(db, monkeypatch):
    import ask_cavnar_tools as tools
    r = _restaurant(db)
    _world(db, r.id)
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 11, 20))
    out = tools._read_events(r.id, days=7)
    saints = [u for u in out["upcoming"] if "Saints" in u["what"]][0]
    assert saints["staffing"]["plan"][0]["role"] == "Bartender PM"
    assert saints["rush"]["pattern_hours_from_kickoff"] == -1


# ── the catalog editor and the owner's follows ─────────────────────────────

def _event(db, ext):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    conn = models.get_conn(db)
    try:
        return dict(conn.execute("SELECT * FROM catalog_events WHERE series_id=? AND external_id=?",
                                 (s["id"], ext)).fetchone())
    finally:
        conn.close()


def test_an_admins_correction_survives_the_daily_season_reload(db):
    wk18 = _event(db, "2026-reg-18")
    got = store.edit_event(wk18["id"], {"event_date": "2027-01-10", "kickoff_local": "19:20", "broadcast": "NBC"},
                           db_path=db)
    assert got["before"]["event_date"] is None and got["after"]["event_date"] == "2027-01-10"
    store.load_bundled(db_path=db)
    again = _event(db, "2026-reg-18")
    assert (again["event_date"], again["kickoff_local"], again["broadcast"], again["is_primetime"]) == \
        ("2027-01-10", "19:20", "NBC", 1)
    # handed back, the season file's values return on the next load
    store.edit_event(wk18["id"], {}, clear=["event_date", "kickoff_local", "broadcast"], db_path=db)
    store.load_bundled(db_path=db)
    assert _event(db, "2026-reg-18")["event_date"] is None


def test_a_bad_correction_is_refused_by_name(db):
    jets = _event(db, "2026-reg-04")
    for change, word in (({"kickoff_local": "7pm"}, "24-hour"), ({"event_date": "10/4/26"}, "YYYY-MM-DD"),
                         ({"status": "won"}, "status"), ({"opponent": "Lions"}, "can be corrected")):
        with pytest.raises(ValueError, match=word):
            store.edit_event(jets["id"], change, db_path=db)
    with pytest.raises(LookupError):
        store.edit_event(999999, {"broadcast": "CBS"}, db_path=db)


def test_an_owner_can_stop_following_and_the_games_leave_at_once(db, monkeypatch):
    import demand_signals
    # set_owner_follow syncs on the restaurant's today: pinned, so the
    # 10/1-12/31 copies stay inside the window whatever the real date
    # (re-audit 2 RX-08: this failed from Feb 2028).
    monkeypatch.setattr(engine, "_today", lambda r=None: date(2026, 10, 1))
    r = _restaurant(db)
    choices = engine.follow_choices(r, today=date(2026, 10, 1), db_path=db)
    assert len(choices) == 1 and choices[0]["following"] and choices[0]["in_reach"]
    assert choices[0]["next"].startswith("Bears vs New York Jets")
    sid = choices[0]["series_id"]
    engine.set_owner_follow(r, sid, False, db_path=db)
    assert not [s for s in demand_signals.upcoming(r.id, "2026-10-01", "2026-12-31", db_path=db)
                if s.get("source") == "events"]
    assert engine.follow_choices(r, db_path=db)[0]["following"] is False
    # auto-follow never overrides the owner's choice
    engine.ensure_follows(r, db_path=db)
    assert store.follows(r.id, db_path=db) == []
    engine.set_owner_follow(r, sid, True, db_path=db)
    assert [s for s in demand_signals.upcoming(r.id, "2026-10-01", "2026-12-31", db_path=db)
            if s.get("source") == "events"]
    with pytest.raises(LookupError):
        engine.set_owner_follow(r, 999999, True, db_path=db)


def test_the_follow_route_is_the_schedule_editors_and_the_list_rides_the_events_card(db, monkeypatch):
    from flask import Flask
    import demand_signals
    import ops
    import strategy_routes
    # The route hands its re-sync to ops' admin pool (re-audit 2 RX-03):
    # inline here, on a pinned today.
    monkeypatch.setattr(ops, "run_admin_task",
                        lambda kind, rid, name, fn, *a, context="", **k: (fn(*a, **k), ("inline", False))[1])
    monkeypatch.setattr(engine, "_today", lambda r=None: date(2026, 10, 1))
    r = _restaurant(db)
    sid = store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]
    app = Flask(__name__)
    owner = {"restaurant_id": r.id, "role": "owner", "id": 1, "username": "erik"}
    with app.test_request_context("/labor/demand-signals"):
        body, status = strategy_routes._do_demand_signals_get(owner)
    assert status == 200 and body["follows"][0]["name"] == "Chicago Bears"
    with app.test_request_context(f"/labor/event-follows/{sid}", method="POST", json={"active": "no"}):
        body, status = strategy_routes._do_event_follow_set(owner, sid)
    assert status == 400
    with app.test_request_context(f"/labor/event-follows/{sid}", method="POST", json={"active": False}):
        body, status = strategy_routes._do_event_follow_set(owner, sid)
    assert status == 200 and body["follows"][0]["following"] is False and body["refreshing"] is True
    assert not [s for s in demand_signals.upcoming(r.id, "2026-10-01", "2026-12-31", db_path=db)
                if s.get("source") == "events"]
    with app.test_request_context(f"/labor/event-follows/{sid}", method="POST", json={"active": True}):
        body, status = strategy_routes._do_event_follow_set({"restaurant_id": r.id, "role": "employee"}, sid)
    assert status == 403
    assert ("/labor/event-follows/<int:series_id>", ["POST"], strategy_routes._do_event_follow_set,
            "event_follow_set") in strategy_routes._ROUTES


def test_the_catalog_editor_is_admin_only_audited_and_moves_every_follower(db, monkeypatch):
    import admin_events
    import admin_routes
    import auth
    from auth import create_session, create_user, init_auth
    from flask import Flask
    import demand_signals
    # The re-sync runs on the restaurant's today: pinned, so Week 18 stays
    # inside the window whatever the real date (re-audit 2 RX-08).
    monkeypatch.setattr(engine, "_today", lambda r=None: date(2026, 10, 1))
    for mod in (auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", models.get_conn, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db, raising=False)
    init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "event-catalog"
    app.register_blueprint(admin_routes.admin_bp)
    r = _restaurant(db)
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db)
    csrf = "ev-csrf"

    def client(admin):
        uid = create_user(hq, "will" if admin else "erik", ("will" if admin else "erik") + "@cavnar.test",
                          "Admin-pass-2026", is_admin=admin, db_path=db)
        c = app.test_client()
        c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db))
        c.set_cookie("csrf_js", csrf)
        return c
    wk18 = _event(db, "2026-reg-18")
    owner = client(False)
    assert owner.post(f"/admin/api/event-catalog/{wk18['id']}", json={"changes": {"event_date": "2027-01-10"}},
                      headers={"X-CSRF": csrf}).status_code in (302, 401, 403)
    c = client(True)
    got = c.get("/admin/api/event-catalog").get_json()
    bears = [s for s in got["series"] if s["slug"] == "nfl-chicago-bears"][0]
    assert bears["followers"] == 1 and len(bears["events"]) == 20 and "event_date" in got["editable"]
    bad = c.post(f"/admin/api/event-catalog/{wk18['id']}", json={"changes": {"kickoff_local": "noon"}},
                 headers={"X-CSRF": csrf})
    assert bad.status_code == 400 and "24-hour" in bad.get_json()["error"]
    # The followers' re-sync runs on the admin job pool (event re-audit
    # P2-10 / X-3); run here inline, as the pool would, to see it land.
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))
    ok = c.post(f"/admin/api/event-catalog/{wk18['id']}", json={"changes": {"event_date": "2027-01-10",
                                                                            "kickoff_local": "15:25"}},
                headers={"X-CSRF": csrf}).get_json()
    assert ok["ok"] and ok["queued"] and ok["followers"] == 1 and ok["after"]["event_date"] == "2027-01-10"
    # the follower's own calendar has Week 18 now, without waiting for 5am
    assert [s for s in demand_signals.upcoming(r.id, "2027-01-10", "2027-01-10", db_path=db)
            if s.get("ref") == f"event:{wk18['id']}"]
    rows = [e for e in admin_events.recent(limit=10) if e.get("event_type") == "event_catalog.edit"]
    assert [x["result"] for x in rows] == ["ok", "refused"]
