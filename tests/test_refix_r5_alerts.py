"""Memory re-audit fix round 9/29/26 (R5, alerts_learn — LOOPS-12): an
alert-type engagement weight (opens and answers of delivered, with a floor)
puts a type the owner ignores last in the combined morning notification and
never in its headline; the morning brief's lines are weighed by the same
effectiveness model Home ranks with."""
import sys

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Alert Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _delivered(rid, alert_type, n):
    conn = models.get_conn()
    for _ in range(n):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,datetime('now','-2 days'))",
                     (rid, alert_type))
    conn.commit()
    conn.close()


def test_engagement_counts_opens_and_answers_with_a_floor():
    import notify
    import rec_ledger
    rid = _rid()
    _delivered(rid, "price_spike", 20)          # never opened, never answered
    _delivered(rid, "food_waste", 12)
    _delivered(rid, "labor_over", 4)            # below the floor
    for i in range(6):
        key = f"food_waste:w{i}"
        rec_ledger.present(rid, key, "food", "alert_push", title="waste")
        rec_ledger.record(rid, key, "dismissed", surface="home", authority="principal")
    eng = notify.alert_engagement(rid)
    assert eng["price_spike"]["low"] is True and eng["price_spike"]["rate"] == 0.0
    assert eng["food_waste"]["engaged"] == 6 and eng["food_waste"]["low"] is False
    assert eng["labor_over"]["low"] is False                      # 4 is below ENGAGEMENT_MIN_DELIVERED


def test_a_low_engagement_type_never_leads_the_morning_notification(monkeypatch):
    import notify
    from push import priority_of
    rid = _rid()
    assert priority_of("labor_over") < priority_of("negative_trend")   # labor would lead by priority
    _delivered(rid, "labor_over", 20)                                   # ...but it is ignored for weeks
    sent = {}
    monkeypatch.setattr(notify, "_deliver_or_hold", lambda rid_, t, sms, subject, html, **kw: sent.update(
        sms=sms, text_recs=kw.get("text_recs")))
    monkeypatch.setattr(notify, "_log_alert", lambda *a, **k: None)
    items = [notify._Pending("labor_over", "labor", "Labor over target", ["Labor over target"], None),
             notify._Pending("negative_trend", "trend", "Reviews trending down", ["Reviews trending down"], None)]
    notify._deliver_combined(rid, items, models.DB_PATH)
    assert "Reviews trending down" in sent["sms"] and "Labor over target" not in sent["sms"]
    assert [i.alert_type for i in items] == ["negative_trend", "labor_over"]      # still in the email, last


def test_the_brief_weighs_lines_between_critical_ones_only(monkeypatch):
    import morning_brief
    import rec_learning
    rid = _rid()

    class Learned:
        def __call__(self, key):
            return ({"a": 0.7, "b": 1.2, "c": 1.0}.get(key, 1.0), [])
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: Learned())
    lines = [{"rec": "crit", "critical": True, "text": "x"}, {"rec": "a", "text": "a"},
             {"rec": "b", "text": "b"}, {"key": "plain", "text": "p"}]
    out = morning_brief._rank_learned(rid, None, lines)
    assert [l.get("rec") or l.get("key") for l in out] == ["crit", "b", "plain", "a"]
    assert out[1]["learned_weight"] == 1.2
    # nothing learned: the order is exactly as built
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: (lambda key: (1.0, [])))
    assert morning_brief._rank_learned(rid, None, lines) == lines
