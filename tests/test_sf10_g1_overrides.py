"""Schedule fix round 10/10/26, workstream G1 — publish warnings the owner
sends past are stored by key and learned from (TRUST-28): the record beside
each warning, the ones always overridden ranked last and listed, a
demotable one moved to a note — never a hard one, never unattended."""
import datetime as dt
import sys

import pytest
from flask import Flask

import auth
import client_api
import schedule_publish
import emails
import labor
import mobile_api
import models
import schedule_overrides as so
import schedule_versions as sv
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07"]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True})
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: emails.SendResult(True))
    return db_path


def _restaurant(db_path):
    rid = create_restaurant(Restaurant(name="Override Co", owner_email="o@x.com"), db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET module_labor=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    models.add_manual_team_member(rid, "Ana", role="Server", db_path=db_path)
    return rid


def _csv():
    return HEADER + "\n" + "\n".join(
        f"{d},{dt.date.fromisoformat(d).strftime('%A')},Ana,Server,11:00am,3:00pm,4," for d in W1)


def _save(db_path, rid):
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, _csv(), [], db_path=db_path)
    sv.append(rid, hid, "generated", _csv(), saved_by="Cavnar AI")
    return hid


def _gate(monkeypatch, rid, blockers):
    """publish_review as the gate builds it, learned from — with these
    blockers on every week."""
    # The gate lives in schedule_publish (ARCH-15); client_api re-exports it.
    import schedule_publish
    monkeypatch.setattr(schedule_publish, "publish_review",
                        lambda r, s=None, unattended=False, today=None: so.annotate(
                            r, {"blockers": [dict(b) for b in blockers], "soft": [], "notes": [], "schedule_id": s},
                            unattended=unattended))


OWNER = {"id": 1, "username": "owner", "role": "owner"}
OVER = {"key": "hours_over", "text": "120h of hourly shifts against a 100h hourly budget — 20h over the ceiling"}
RULE = {"key": "rule:min_rest:ana:2026-10-06:11:00am", "text": "Ana — Tuesday 11:00am: under 10 hours' rest"}


def test_kinds_and_which_are_hard():
    assert so.kind_of("rule:min_rest:ana:2026-10-06:11:00am") == "rule:min_rest"
    assert so.kind_of("saved:Over budget by 3h") == "saved"
    assert so.kind_of("hours_over") == "hours_over"
    assert so.kind_of("late_change:2026-10-06") == "late_change"
    assert not so.hard("hours_over") and not so.hard("saved")
    for k in ("rule:min_rest", "input:availability", "stage:manager", "notice_short", "late_change",
              "quality_unchecked", "roster:no_manager", "soft:minor_hours"):
        assert so.hard(k), k


def test_each_override_is_kept_by_key_with_its_version_and_reason(db, monkeypatch):
    rid = _restaurant(db)
    _gate(monkeypatch, rid, [OVER])
    hid = _save(db, rid)
    out, status = client_api._publish_schedule(rid, hid, actor=OWNER, acknowledge=["hours_over"],
                                               override_reasons={"hours_over": "Bears home game"})
    assert status == 200 and out["ok"]
    conn = models.get_conn(db)
    row = conn.execute("SELECT * FROM schedule_publish_overrides WHERE history_id=?", (hid,)).fetchone()
    conn.close()
    assert (row["key"], row["kind"], row["reason"]) == ("hours_over", "hours_over", "Bears home game")
    assert row["overridden_at"] and row["version"] == out["version"] and row["actor"] == "owner"


def test_a_warning_the_owner_always_overrides_is_demoted_but_a_rule_never_is(db, monkeypatch):
    rid = _restaurant(db)
    _gate(monkeypatch, rid, [RULE, OVER])
    for _ in range(5):
        hid = _save(db, rid)
        out, status = client_api._publish_schedule(rid, hid, actor=OWNER, acknowledge=[RULE["key"], "hours_over"])
        assert status == 200 and out["ok"]
    st = so.stats(rid)
    assert st["hours_over"]["overridden"] == 5 and st["hours_over"]["demoted"]
    assert st["rule:min_rest"]["overridden"] == 5 and not st["rule:min_rest"]["demoted"], "a hard rule was quieted"
    hid = _save(db, rid)
    review = schedule_publish.publish_review(rid, hid)
    assert [b["key"] for b in review["blockers"]] == [RULE["key"]]
    assert review["blockers"][0]["overridden_before"]["weeks"] == 5
    assert any(n.get("demoted") and "5 of 5 weeks" in n["text"] for n in review["notes"])
    assert {a["kind"] for a in review["always_overridden"]} == {"hours_over", "rule:min_rest"}
    # The send now needs only the rule acknowledged.
    out, status = client_api._publish_schedule(rid, hid, actor=OWNER, acknowledge=[RULE["key"]])
    assert status == 200 and out["ok"]
    # Never for an unattended send: the automation's gate is unchanged.
    hid = _save(db, rid)
    review = schedule_publish.publish_review(rid, hid, unattended=True)
    assert "hours_over" in [b["key"] for b in review["blockers"]]


def test_a_warning_sometimes_fixed_is_not_demoted(db, monkeypatch):
    rid = _restaurant(db)
    other = {"key": "saved:Thursday is short a cook", "text": "Thursday is short a cook"}
    _gate(monkeypatch, rid, [OVER, other])
    for i in range(5):
        hid = _save(db, rid)
        if i % 2:
            out, _s = client_api._publish_schedule(rid, hid, actor=OWNER, acknowledge=[OVER["key"], other["key"]])
            assert out["ok"]
        else:
            # Shown (a 409), then fixed rather than sent past.
            out, status = client_api._publish_schedule(rid, hid, actor=OWNER, acknowledge=[])
            assert status == 409
    st = so.stats(rid)
    assert st["hours_over"]["shown"] == 5 and st["hours_over"]["overridden"] == 2 and not st["hours_over"]["demoted"]


def test_the_publish_route_takes_overrides_with_reasons(db, monkeypatch):
    rid = _restaurant(db)
    _gate(monkeypatch, rid, [OVER])
    hid = _save(db, rid)
    auth.init_auth(db_path=db)
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    r = app.test_client().post("/mobile/api/labor/publish-schedule",
                               json={"schedule_id": hid, "overrides": [{"key": "hours_over", "reason": "Patio open"}]},
                               headers={"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"})
    assert r.status_code == 200 and r.get_json()["ok"]
    conn = models.get_conn(db)
    reason = conn.execute("SELECT reason FROM schedule_publish_overrides WHERE history_id=?", (hid,)).fetchone()[0]
    conn.close()
    assert reason == "Patio open"
