"""Event Intelligence re-audit, the merge's own follow-ups (10/1/26): the
handoffs between fix rounds C and D, applied after both landed.

  X-8    the report's day-after prep follows the one item-mix rule;
  P4-11  Food Cost's game week leads with the measured game of a shared date;
  P2-04  the pre-shift names a game on the restaurant's clock;
  X-2    Ask's read_events shares one call's reads.
"""
import inspect

from event_intel import engine, gameday


def _tomorrow():
    return {"tomorrow": {"items": [{"kind": "game_prep", "text": "Prep for about 60 Wings"},
                                   {"kind": "event", "text": "Bears vs Packers"}]}}


def test_the_report_drops_game_prep_only_when_both_item_mix_views_are_withheld():
    from dsr import access
    kinds = lambda t: [i["kind"] for i in t["items"]]
    assert "game_prep" not in kinds(access.tomorrow_for(_tomorrow(), None, "x", withheld=["labor", "food"]))
    assert "game_prep" in kinds(access.tomorrow_for(_tomorrow(), None, "x", withheld=["labor"]))
    assert "game_prep" in kinds(access.tomorrow_for(_tomorrow(), None, "x", withheld=["food"]))


def test_the_game_week_leads_with_the_measured_game_and_names_the_other(monkeypatch):
    from datetime import date
    from event_intel import store
    bulls = {"id": 1, "series_id": 1, "event_date": "2026-11-08", "status": "scheduled"}
    bears = {"id": 2, "series_id": 2, "event_date": "2026-11-08", "status": "scheduled"}
    monkeypatch.setattr(store, "follows", lambda *a, **k: [{"series_id": 1}, {"series_id": 2}])
    monkeypatch.setattr(store, "dismissed", lambda *a, **k: set())
    monkeypatch.setattr(store, "events_for", lambda *a, **k: [bulls, bears])
    monkeypatch.setattr(engine, "headline", lambda *a, **k: True)
    monkeypatch.setattr(engine, "effect_for", lambda rid, e, **k: {"n": 2} if e["id"] == 2 else None)
    monkeypatch.setattr(engine, "restaurant_clock", lambda *a, **k: "America/Chicago")
    monkeypatch.setattr(engine, "describe", lambda e, **k: "Bears vs Packers" if e["id"] == 2 else "Bulls vs Heat")
    monkeypatch.setattr(gameday, "item_mix", lambda *a, **k: None)
    monkeypatch.setattr(gameday, "order_bump", lambda *a, **k: None)
    note = gameday.week_note(1, today=date(2026, 11, 5))
    assert note["event_id"] == 2 and note["others"] == [1]
    assert note["text"].endswith("Also that day: Bulls vs Heat.")


def test_the_preshift_and_ask_read_games_on_the_restaurants_clock_in_one_read():
    import ask_cavnar_tools
    import preshift
    src = inspect.getsource(preshift)
    assert "describe(e, with_date=False, tz=engine.restaurant_clock(" in src
    assert "engine.one_read()" in inspect.getsource(ask_cavnar_tools._read_events)


def test_home_lists_alerts_by_the_same_audience_rule_as_the_bell_and_the_push():
    # Event re-audit 2, R3-07: one rule (client_api.sees_alert → push.audience_of).
    import home_brief
    src = inspect.getsource(home_brief)
    assert "sees_alert(current_user, alert_type)" in src
    assert "_NOTIFICATION_MODULE.get(alert_type" not in src


# ── event re-audit 2: the merge's follow-ups between W1–W5 ─────────────────

def test_peers_give_no_figure_for_a_playoff_or_special_game(monkeypatch):
    from event_intel import peers
    # Refused before any peer row is read (peer_effect swallows errors, so
    # record the read rather than raise).
    reads = []
    monkeypatch.setattr(peers, "_memoised", lambda *a, **k: reads.append(a) or None)
    for st in ("postseason", "special"):
        assert peers.peer_effect(1, {"home_away": "home", "series_id": 1, "season_type": st,
                                     "attributes": {}}) is None, st
    assert reads == []


def test_the_brief_reads_guest_text_by_the_one_rule_and_the_one_send_time():
    import morning_brief
    from event_intel import playbook
    assert "gameday.guest_text_visible(restaurant, denied=denied)" in inspect.getsource(morning_brief._game_line)
    src = inspect.getsource(playbook.alert)
    assert "gameday.plan_ahead(" in src and "gameday.send_plan(" not in src


def test_a_label_whose_effect_applies_is_said_by_that_one_figure(monkeypatch):
    lab = {"segment": "Bears home games", "n": 4, "median_lift_pct": 30.0, "applies": True}
    monkeypatch.setattr(engine, "_judged_by_its_label", lambda *a, **k: False)
    monkeypatch.setattr(engine, "_label_effect", lambda *a, **k: lab)
    monkeypatch.setattr(engine, "past_games", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no segments")))
    assert engine.effect_for(1, {"series_id": 1, "home_away": "home"}) is lab
    monkeypatch.setattr(engine, "_label_effect", lambda *a, **k: dict(lab, applies=False))
    monkeypatch.setattr(engine, "past_games", lambda *a, **k: [])
    assert engine.effect_for(1, {"series_id": 1, "home_away": "home"}) is None   # segments, none yet


def test_upcoming_dates_default_to_the_restaurants_own_today(monkeypatch):
    from datetime import datetime
    import demand_signals
    import time_utils
    seen = {}
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: datetime(2026, 10, 2, 0, 30))

    class C:
        def execute(self, sql, args):
            seen["args"] = args
            return self

        def fetchall(self):
            return []

        def close(self):
            pass
    monkeypatch.setattr(demand_signals, "get_conn", lambda *a, **k: C())
    demand_signals.upcoming(5)
    assert seen["args"] == (5, "2026-10-02", "2026-12-01")


def test_stamps_job_history_and_the_events_card_follow_the_merge():
    import admin_routes
    import event_memory
    src = inspect.getsource(event_memory)
    assert src.count("when = at or _restaurant_now(restaurant_id)") == 2
    assert '"event_sync_one"' in inspect.getsource(admin_routes) and '"event_catalog_resync"' in inspect.getsource(admin_routes)
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
    assert "function _evReload(d){loadSignals();if(d&&d.refreshing)setTimeout(loadSignals,2500);}" in html
