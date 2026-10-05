"""Issue texts off (Erik, Simple EJ's, 10/5/26): issues still open and are
assigned, reach people by push and the bell, and nobody is texted — an
escalation included."""
import pytest

import auth
import models
from models import create_restaurant, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import issues, notify
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for m in (models, auth, issues, notify):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)


@pytest.fixture
def setup(db_path, monkeypatch):
    import issues, notify, push
    rid = create_restaurant(Restaurant(name="Texts Co", owner_email="t@x.test"), db_path=db_path)
    cid = notify.add_alert_contact(rid, "Erik", "+13125550101", sms_consent=True)
    issues.set_routing(rid, "manager", cid)
    texts, pushes = [], []
    monkeypatch.setattr(issues, "_text", lambda phone, msg, r: texts.append(msg) or type(
        "R", (), {"ok": True, "status": "sent", "error": None, "permanent": False})())
    monkeypatch.setattr(push, "fire_push", lambda rid, kind, title, body, **k: pushes.append((kind, title)))
    monkeypatch.setattr(notify, "alert_audience", lambda *a, **k: None)
    return rid, texts, pushes


def test_on_by_default_a_new_issue_is_texted(setup):
    import issues
    rid, texts, pushes = setup
    assert issues.texts_on(rid) is True
    issues.create_issue(rid, "review", "2★ review needs follow-up", source_key="review:1")
    assert len(texts) == 1 and pushes == []


def test_off_the_issue_still_opens_and_is_pushed_once_never_texted(setup):
    import issues
    rid, texts, pushes = setup
    models.update_restaurant(rid, {"issue_texts": 0})
    issue, _token = issues.create_issue(rid, "coverage", "Mia Martin hasn't clocked in", source_key="coverage:x")
    assert issue["status"] == "open" and issue["assignee_name"] == "Erik", "still assigned"
    assert texts == [] and pushes == [("coverage", "Mia Martin hasn't clocked in")]
    assert issues._notify(issue["id"]) is False and len(pushes) == 1, "claimed: never pushed twice"


def test_off_nobody_is_texted_on_escalation(setup, monkeypatch):
    import issues, notify
    rid, texts, pushes = setup
    esc = notify.add_alert_contact(rid, "Jim", "+13125550102", sms_consent=True)
    issues.set_routing(rid, "escalation", esc, escalate_after_minutes=1)
    models.update_restaurant(rid, {"issue_texts": 0})
    issues.create_issue(rid, "labor", "Labor 42% against a 32% target", source_key="labor:w")
    c = models.get_conn(); c.execute("UPDATE ops_issues SET notified_at=datetime('now','-3 hours')"); c.commit(); c.close()
    called = []
    monkeypatch.setattr(issues, "_escalate", lambda *a, **k: called.append(1) or True)
    assert issues.tick()["escalated"] == 0 and called == [] and texts == []


def test_the_switch_is_a_restaurant_setting_with_its_four_touch_points(db_path):
    rid = create_restaurant(Restaurant(name="Switch Co", owner_email="s@x.test"), db_path=db_path)
    assert models.get_restaurant(rid).issue_texts == 1
    models.update_restaurant(rid, {"issue_texts": 0})
    assert models.get_restaurant(rid).issue_texts == 0
