"""_do_get_notifications() label mapping — a prior version of this dict keyed
on prefixed strings ("alert_1star") while notify.py._log_alert() always wrote
unprefixed values ("1star"), so the lookup silently missed every time and
every notification fell back to the raw internal string instead of a
human-readable label."""
import client_api
import models
from models import create_restaurant, Restaurant


def _redirect(monkeypatch, db_path):
    real_get_conn = models.get_conn
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Label Test Co", owner_email="l@x.com"), db_path=db_path)


def test_notification_labels_match_actual_unprefixed_alert_types(monkeypatch, db_path):
    _redirect(monkeypatch, db_path)
    rid = _restaurant(db_path)
    conn = models.get_conn(db_path)
    for alert_type in ("1star", "neg_spike", "health", "no_response", "labor_over"):
        conn.execute(
            "INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?, ?)",
            (rid, alert_type),
        )
    conn.commit()
    conn.close()

    payload, status = client_api._do_get_notifications(rid)

    assert status == 200
    assert payload["ok"] is True
    labels = {item["type"]: item["label"] for item in payload["notifications"]}
    assert labels["1star"] == "1★ review received"
    assert labels["neg_spike"] == "Negative review spike"
    assert labels["health"] == "Health/safety mention"
    assert labels["no_response"] == "Unresponded review (48h)"
    assert labels["labor_over"] == "Labor % over target"
    # Regression guard: none of these should fall back to the raw internal
    # string (which is what happened when the dict keys were prefixed).
    for item in payload["notifications"]:
        assert item["label"] != item["type"]
