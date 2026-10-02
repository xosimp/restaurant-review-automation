"""A notification nobody can receive spends no briefing slot (10/1/26).

The bell row a briefing writes counts against the day's budget
(notify.briefing_allowed). strategy_jobs._reach and the push-only slot jobs
(the pre-dinner pulse, the quiet-night heads-up) wrote it before anyone's own
mute was checked and ignored fire_push's answer, so a push every recipient
muted crowded out a later briefing. One audience rule now decides first
(strategy_jobs.deliverable_audience with the type), and a row no device took
comes back out (notify.withdraw_notification) — the event push's fix
(event re-audit 2, R3-06), applied to every slot sender.
"""
import types

import pytest

import morning_brief
import notify
import preferences
import push
import strategy_jobs


@pytest.fixture
def world(monkeypatch):
    calls = {"rows": [], "withdrawn": [], "pushed": [], "emailed": []}
    people = [{"id": 1, "email": "a@x.com"}, {"id": 2, "email": "b@x.com"}, {"id": 3, "email": "c@x.com"}]
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(notify, "alert_permissions", lambda types_: [])
    monkeypatch.setattr(notify, "record_notification",
                        lambda rid, t, db_path=None, **k: calls["rows"].append(t) or 77)
    monkeypatch.setattr(notify, "withdraw_notification",
                        lambda rid, aid, db_path=None: calls["withdrawn"].append(aid) or True)
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: people)
    # Logins 1 and 2 have the app; 3 has only email.
    monkeypatch.setattr(push, "get_device_tokens",
                        lambda rid, db_path=None, for_delivery=False: [{"user_id": 1}, {"user_id": 2}])
    monkeypatch.setattr(push, "fire_push",
                        lambda rid, t, title, body, data=None, db_path=None, user_ids=None, **k:
                        calls["pushed"].append(set(user_ids)) or len(user_ids))
    import models
    monkeypatch.setattr(models, "get_restaurant",
                        lambda rid, db_path=None: types.SimpleNamespace(billing_status="active", name="R",
                                                                        location_name=None))
    import emails
    monkeypatch.setattr(emails, "deliver", lambda **k: calls["emailed"].append(k["payload"]["to"][0])
                        or types.SimpleNamespace(ok=True))
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: False)
    return calls, people


def _mute(monkeypatch, muted):
    monkeypatch.setattr(preferences, "push_allowed",
                        lambda uid, rid, t, now_local=None, db_path=None, _cache=None: uid not in muted)


def test_the_audience_rule_leaves_out_a_login_who_muted_the_type(world, monkeypatch):
    _mute(monkeypatch, {2})
    assert strategy_jobs.deliverable_audience(5, {1, 2, 3}) == {1, 2}            # phones, no type
    assert strategy_jobs.deliverable_audience(5, {1, 2, 3}, alert_type="closing_summary") == {1}


def test_a_reach_everyone_muted_writes_no_row_and_emails_nobody_who_has_the_app(world, monkeypatch):
    calls, people = world
    _mute(monkeypatch, {1, 2})
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: people[:2])
    assert strategy_jobs._reach(5, "closing_summary", "t", "b", {}, None) == 0
    assert calls["rows"] == [] and calls["pushed"] == [] and calls["emailed"] == []


def test_a_muted_login_is_not_pushed_and_not_emailed_instead(world, monkeypatch):
    calls, _ = world
    _mute(monkeypatch, {2})
    assert strategy_jobs._reach(5, "closing_summary", "t", "b", {}, None) == 2   # 1 by push, 3 by email
    assert calls["rows"] == ["closing_summary"] and calls["pushed"] == [{1}]
    assert calls["emailed"] == ["c@x.com"] and calls["withdrawn"] == []


def test_a_push_no_device_took_and_no_email_takes_its_row_back(world, monkeypatch):
    calls, people = world
    _mute(monkeypatch, set())
    monkeypatch.setattr(morning_brief, "recipients", lambda rid, db_path=None, include_opted_out=False: people[:2])
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: 0)
    assert strategy_jobs._reach(5, "closing_summary", "t", "b", {}, None) == 0
    assert calls["rows"] == ["closing_summary"] and calls["withdrawn"] == [77]


def test_every_slot_sender_asks_the_one_audience_rule_with_its_type():
    import inspect
    from event_intel import gameday
    src = inspect.getsource(strategy_jobs)
    assert 'db_path, alert_type="intraday_pulse")' in src
    assert 'db_path, alert_type="demand_opportunity")' in src
    assert "alert_type=PUSH_TYPE)" in inspect.getsource(gameday.run_event_push)
    reach = inspect.getsource(strategy_jobs._reach)
    assert reach.index("deliverable_audience(") < reach.index("notify.record_notification(")
    assert "notify.withdraw_notification(" in reach
