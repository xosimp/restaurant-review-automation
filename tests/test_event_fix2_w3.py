"""Event re-audit 2 fixes, gameday, push and Ask (W3), 10/1/26.

  R3-01           a measured figure that had to count a holiday night is not
                  a reason to push (big_game falls to the clean-last-game rule)
  R3-02 / R2-06   Ask never offers a guest-text time or an email day already
                  gone (gameday.plan_ahead, the brief's and the push's test)
  R3-03 / RX-04   one rule for who reads the guest-text timing
                  (gameday.guest_text_visible: the Marketing module and the
                  login's Marketing view) — the brief, the push and Ask
  R3-04           the push's guest-text sentence goes only to the recipients
                  that rule lets read it
  R3-05 / R4-04   a game the restaurant removed is never counted by the
                  season's money or the game-night review window
  R3-06           a push every recipient muted writes no budgeted bell row
                  and is not "sent"
  R3-07           the big-game bell row follows the push's audience (Labor)
  R4-02           no peer figure for a home game at another ground

The clock is pinned: nothing here depends on the real calendar date.
"""
from datetime import date, datetime

import pytest

import models
from event_intel import engine, gameday, peers, store
from models import get_restaurant, update_restaurant
from test_event_fix_d import CLOCK, _confound, _event, _push_world, db  # noqa: F401  (db is the fixture)
from test_event_playbook import _restaurant, _saints, _world


def _rows_of(db, rid, alert_type="event_ahead"):
    c = models.get_conn(db)
    try:
        return c.execute("SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND alert_type=?",
                         (rid, alert_type)).fetchone()[0]
    finally:
        c.close()


# ── R3-01: a mixed figure is not big ───────────────────────────────────────

def test_a_figure_that_had_to_count_a_holiday_night_is_not_a_reason_to_push(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(80.0, 10.0))           # 9/20 +80 (Christmas-like), 10/4 +10, clean
    _confound(db, r.id, "2026-09-20")
    eff = engine.effect_for(r.id, _saints(db), db_path=db)
    assert eff["confounded"] and eff["median_lift_pct"] >= gameday.BIG_LIFT     # the mixed median: +45
    assert gameday.big_game(r.id, _saints(db), db_path=db) == (False, None)
    # The same nights, both clean: a measured +45 is big.
    c = models.get_conn(db)
    try:
        c.execute("UPDATE event_outcomes SET confounded=0 WHERE restaurant_id=?", (r.id,))
        c.commit()
    finally:
        c.close()
    big, words = gameday.big_game(r.id, _saints(db), db_path=db)
    assert big and words.startswith("Bears home games have run +45%")


def test_a_mixed_figure_falls_to_the_last_clean_game_of_its_kind(db):
    r = _restaurant(db)
    _world(db, r.id, lifts=(80.0, 60.0))           # the holiday night first, then a clean +60
    _confound(db, r.id, "2026-09-20")
    big, words = gameday.big_game(r.id, _saints(db), db_path=db)
    assert big and "ran +60% against a usual Sunday — one night." in words


# ── R3-03 / RX-04 / R3-04: one rule for who reads the guest text ──────────

def test_one_rule_says_who_reads_the_guest_text(db, monkeypatch):
    import permissions
    from permissions import LABOR_VIEW
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "shift", frozenset({LABOR_VIEW}))
    r = _restaurant(db)
    off = _restaurant(db, name="No Marketing Co", module_marketing=0)
    vis = gameday.guest_text_visible
    assert vis(r) and vis(r, denied=set()) and vis(r, user={"role": "manager"})
    assert not vis(r, denied={"marketing"}) and not vis(r, user={"role": "shift"})
    assert vis(r, user={"role": "shift", "is_admin": True})
    assert not vis(off) and not vis(off, denied=set()) and not vis(off, user={"role": "owner"})
    assert not vis(None)


def test_ask_gives_no_guest_text_where_marketing_is_off_or_not_the_logins(db, monkeypatch):
    import ask_cavnar_tools as tools
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 11, 20))

    def saints(rid, viewer=None):
        return [u for u in tools._read_events(rid, days=3, _viewer=viewer)["upcoming"] if "Saints" in u["what"]][0]
    r = _restaurant(db)
    assert saints(r.id)["reach_guests"]["text"] == "Sunday around 9am, 3 hours before kickoff"
    assert saints(r.id, tools.viewer_restaurant(r, {"id": 1, "role": "manager"}))["reach_guests"]
    off = _restaurant(db, name="No Marketing Co", module_marketing=0)
    assert "reach_guests" not in saints(off.id)
    assert "reach_guests" not in saints(off.id, tools.viewer_restaurant(off, {"id": 2, "role": "owner"}))
    view = tools.viewer_restaurant(r, None)
    view._ask_denied = frozenset({"marketing"})
    assert "reach_guests" not in saints(r.id, view)


def test_the_push_carries_the_guest_text_only_to_logins_who_may_read_it(db, monkeypatch):
    import morning_brief
    import notify
    import permissions
    from permissions import LABOR_VIEW
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "shift", frozenset({LABOR_VIEW}))
    r, sent, _ = _push_world(db, monkeypatch)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: [
        {"id": 1, "role": "owner"}, {"id": 2, "role": "shift"}])
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    out = gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert out["sent"] == 1 and len(sent) == 2
    by_who = {frozenset(k["user_ids"]): a[3] for a, k in sent}
    assert "Guest text, as a starting rule" in by_who[frozenset({1})]
    assert "Guest text" not in by_who[frozenset({2})]
    assert by_who[frozenset({2})].startswith("Bears home games have run +25%")
    assert sent[0][1]["data"]["alert_id"] == sent[1][1]["data"]["alert_id"]       # one bell row
    assert _rows_of(db, r.id) == 1


# ── R3-02 / R2-06: never a time already gone ───────────────────────────────

def test_ask_never_offers_a_text_time_or_an_email_day_already_gone(db, monkeypatch):
    import ask_cavnar_tools as tools
    r = _restaurant(db)

    def saints(now):
        CLOCK["now"] = now
        monkeypatch.setattr(tools, "_local_today_of", lambda rid: now.date())
        return [u for u in tools._read_events(r.id, days=3)["upcoming"] if "Saints" in u["what"]][0]
    ahead = saints(datetime(2026, 11, 20, 10, 0))["reach_guests"]
    assert ahead["text"] == "Sunday around 9am, 3 hours before kickoff" and ahead["email"].startswith("Saturday 11/21/26")
    morning = saints(datetime(2026, 11, 22, 7, 30))["reach_guests"]           # game day, before the text
    assert morning["text"].startswith("Sunday around 9am") and "email" not in morning
    assert "reach_guests" not in saints(datetime(2026, 11, 22, 10, 0))         # the text time has gone
    plan = gameday.plan_ahead(r.id, _saints(db), tz=r)
    assert plan is None


# ── R3-05 / R4-04: a removed game counts nowhere ───────────────────────────

def test_the_seasons_money_never_counts_a_game_the_restaurant_removed(db):
    r = _restaurant(db)
    _world(db, r.id)
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    before = gameday.season_value(r.id, s["id"], today=date(2026, 11, 20), db_path=db)
    store.dismiss(r.id, _event(db, "2026-10-04")["id"], db_path=db)
    after = gameday.season_value(r.id, s["id"], today=date(2026, 11, 20), db_path=db)
    assert after["played"] == before["played"] - 1
    assert after["measured"] == before["measured"] - 1
    assert "2026-10-04" not in {g["date"] for g in after["games"]}


def test_the_game_night_review_window_never_counts_a_removed_game(db):
    import json
    from event_intel import reviews
    r = _restaurant(db)
    c = models.get_conn(db)
    try:
        for day, n in (("2026-09-21", 10), ("2026-09-29", 10), ("2026-09-03", 20)):
            for i in range(n):
                c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, "
                          "fetched_at, processed, categories, sentiment) VALUES (?,?,?,?,?,?,?,1,?,?)",
                          (r.id, "google", f"{day}-{i}", 2, "x", day, day, json.dumps(["service"]), "negative"))
        c.commit()
    finally:
        c.close()
    before = reviews.game_night_reviews(r.id, today=date(2026, 10, 1), days=60, db_path=db)
    store.dismiss(r.id, _event(db, "2026-09-20")["id"], db_path=db)
    store.dismiss(r.id, _event(db, "2026-09-28")["id"], db_path=db)
    after = reviews.game_night_reviews(r.id, today=date(2026, 10, 1), days=60, db_path=db)
    assert before and before["game_reviews"] == 20
    assert after is None or after["game_reviews"] < before["game_reviews"]


# ── R3-06: a push nobody gets is not a briefing ────────────────────────────

def test_a_push_every_recipient_muted_writes_no_bell_row_and_is_not_sent(db, monkeypatch):
    import notify
    import preferences
    r, sent, claims = _push_world(db, monkeypatch, marketing=0)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(preferences, "login_overrides",
                        lambda uid, rid, db_path=None: {"push_muted_types": ["event_ahead"]})
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    out = gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert out["sent"] == 0 and out["skipped"] == 1 and not sent
    assert _rows_of(db, r.id) == 0 and not claims


def test_a_push_queued_for_no_device_takes_its_row_back(db, monkeypatch):
    import notify
    import push
    r, sent, claims = _push_world(db, monkeypatch, marketing=0)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: sent.append((a, k)) or 0)
    CLOCK["now"] = datetime(2026, 11, 21, 15, 0)
    out = gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert sent and out["sent"] == 0 and _rows_of(db, r.id) == 0
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: sent.append((a, k)) or 1)
    claims.clear()
    out = gameday.run_event_push(db_path=db, restaurants=[get_restaurant(r.id, db_path=db)])
    assert out["sent"] == 1 and _rows_of(db, r.id) == 1


# ── R3-07: the bell follows the push's audience ────────────────────────────

def test_the_big_game_bell_row_shows_only_to_the_logins_it_was_pushed_to(db, monkeypatch):
    import ask_cavnar_tools as tools
    import client_api
    import notify
    import permissions
    import push
    from permissions import DASHBOARD_ACCESS, MARKETING_VIEW
    monkeypatch.setitem(permissions.ROLE_PERMISSIONS, "marketer", frozenset({DASHBOARD_ACCESS, MARKETING_VIEW}))
    r = _restaurant(db)
    notify.record_notification(r.id, "event_ahead", db_path=db, ref_kind="catalog_event", ref_id=_saints(db)["id"])
    assert push.audience_of("event_ahead") == "labor" and push.module_of("event_ahead") == "ask"
    owner, marketer = {"id": 1, "role": "owner"}, {"id": 2, "role": "marketer"}

    def types(viewer):
        body, _ = client_api._do_get_notifications(r.id, viewer=viewer)
        return [x["type"] for x in body["notifications"]]
    assert "event_ahead" in types(owner) and "event_ahead" not in types(marketer)
    row = [x for x in client_api._do_get_notifications(r.id, viewer=owner)[0]["notifications"]
           if x["type"] == "event_ahead"][0]
    assert row["module"] == "ask"                                            # still opens Ask
    assert client_api.notification_visibility(owner)("event_ahead")
    assert not client_api.notification_visibility(marketer)("event_ahead")   # the badge
    assert not tools.alert_visible(tools.viewer_restaurant(r, marketer), "event_ahead")
    assert tools.alert_visible(tools.viewer_restaurant(r, owner), "event_ahead")
    # The push's own audience is the same rule.
    import morning_brief
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: [
        owner, marketer])
    assert gameday._labor_logins(r.id, db) == {1}


# ── R4-02: no peer figure for a game at another ground ────────────────────

def test_a_home_game_at_another_ground_gets_no_peer_figure(db, monkeypatch):
    from intelligence.benchmarks import MIN_QUARTILE_N
    from models import Restaurant, create_restaurant
    from intelligence import jobs
    game = _saints(db)
    viewer = _restaurant(db)
    for i in range(MIN_QUARTILE_N):
        rid = create_restaurant(Restaurant(name=f"Tap {i}", owner_email=f"o{i}@x{i}.com", timezone="America/Chicago"),
                                db_path=db)
        update_restaurant(rid, {"latitude": 41.9142, "longitude": -88.3087}, db_path=db)
        engine.ensure_follows(get_restaurant(rid, db_path=db), db_path=db)
        c = models.get_conn(db)
        try:
            for d, lift in zip(("2026-09-20", "2026-09-28", "2026-10-04"), (20 + i, 30 + i, 10 + i)):
                c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, lift_pct) "
                          "VALUES (?,?,?,?,?,?)", (rid, d, date.fromisoformat(d).strftime("%A"), "event",
                                                   "bears soldier field", lift))
            c.commit()
        finally:
            c.close()
        peers.store_member_effects(rid, db_path=db)
    peers.invalidate()
    try:
        assert peers.peer_effect(viewer.id, game, db_path=db)                # the home ground: a figure
        away = dict(game, venue="SeatGeek Stadium", attributes={"alt_venue": True})
        assert store.alt_venue(away)
        assert peers.peer_effect(viewer.id, away, db_path=db) is None
    finally:
        peers.invalidate()
