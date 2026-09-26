"""The bell's badge and list count one set, and urgent rows can resolve.

1. The badge (`unread-count`, push._badge_for) and the list counted
   different sets: the list showed the newest 40 without the "born" rule,
   the badge counted every unread row ever fired with it. 46 rows, every
   listed row opened -> the list read 0 unread (so no "Mark all read") and
   the badge read 6, forever. Both now read models.notification_unread_rule
   over the same window (models.NOTIFICATION_WINDOW).
2. `resolved` was `review_id and ...`, so an issue, a coverage gap or a
   stock-out stayed urgent forever. It now reads the subject where it is
   knowable (the row's ref, a newer critical_low) and, where it is not, an
   opened urgent row is handled (`resolves_on_open`).
3. notify._log_alert drops the ref only when the column is missing.
"""
import sqlite3

import pytest

import auth
import client_api
import issues
import models
import notify
import push
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)


def _restaurant(db_path, name="Bell Co"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _user(db_path, rid, created_at="2000-01-01 00:00:00", name="owner"):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, created_at) "
                       "VALUES (?,?,?,?,?)", (rid, name, f"{name}@x.test", "x", created_at))
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "role": "owner", "is_admin": 0}


def _alert(db_path, rid, alert_type="5star", fired_at=None, ref_kind=None, ref_id=None):
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO alert_log (restaurant_id, alert_type, fired_at, ref_kind, ref_id) "
        "VALUES (?,?,COALESCE(?, datetime('now')),?,?)", (rid, alert_type, fired_at, ref_kind, ref_id))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def _list(rid, viewer):
    body, status = client_api._do_get_notifications(rid, viewer=viewer)
    assert status == 200 and body["ok"], body
    return body["notifications"]


def _badge(viewer):
    return client_api._do_notifications_unread(viewer)[0]


def _open(db_path, rid, viewer, n):
    models.record_notification_open(rid, n["type"], user_id=viewer["id"], db_path=db_path, alert_log_id=n["id"])


# ── 1. One definition of unread ──────────────────────────────────────────────

def test_opening_every_listed_row_clears_the_badge(db_path):
    """The audit's probe: 46 rows, open every listed row."""
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    for i in range(46):
        _alert(db_path, rid, fired_at=f"2026-09-{1 + i // 24:02d} {i % 24:02d}:00:00")
    rows = _list(rid, viewer)
    assert len(rows) == models.NOTIFICATION_WINDOW
    assert _badge(viewer)["count"] == sum(1 for n in rows if n["unread"]) == models.NOTIFICATION_WINDOW
    for n in rows:
        _open(db_path, rid, viewer, n)
    assert not any(n["unread"] for n in _list(rid, viewer))
    assert _badge(viewer)["count"] == 0, "the badge counted rows the list never shows"
    # The phone's app icon (push._badge_for) reads the same rule and window.
    assert models.unread_notification_count(viewer["id"], rid, db_path) == 0
    assert push._badge_for({"user_id": viewer["id"], "restaurant_id": rid}, db_path) == 0


def test_the_badge_is_the_lists_unread_count_under_any_mix(db_path):
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    ids = [_alert(db_path, rid, fired_at=f"2026-09-10 {h:02d}:00:00") for h in range(10)]
    models.mark_notifications_seen(viewer["id"], rid, db_path)
    fresh = [_alert(db_path, rid, fired_at="2099-01-01 00:00:00"),
             _alert(db_path, rid, fired_at="2099-01-02 00:00:00")]
    rows = _list(rid, viewer)
    _open(db_path, rid, viewer, [n for n in rows if n["id"] == fresh[0]][0])
    rows = _list(rid, viewer)
    unread = {n["id"] for n in rows if n["unread"]}
    assert unread == {fresh[1]} and ids[0] not in unread
    assert _badge(viewer)["count"] == 1 == models.unread_notification_count(viewer["id"], rid, db_path)


def test_a_new_co_owner_sees_no_history_as_unread_in_the_list_either(db_path):
    """The badge applied the born rule and the list did not: a co-owner
    invited today saw the restaurant's whole history unread in the list."""
    rid = _restaurant(db_path)
    _alert(db_path, rid, fired_at="2026-01-01 09:00:00")
    _alert(db_path, rid, fired_at="2026-01-02 09:00:00")
    newcomer = _user(db_path, rid, created_at="2026-06-01 00:00:00", name="jim")
    after = _alert(db_path, rid, fired_at="2026-06-02 09:00:00")
    rows = _list(rid, newcomer)
    assert {n["id"] for n in rows if n["unread"]} == {after}
    assert _badge(newcomer)["count"] == 1


def test_mark_all_read_clears_what_the_badge_counts(db_path):
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    for h in range(5):
        _alert(db_path, rid, fired_at=f"2026-09-10 {h:02d}:00:00")
    assert _badge(viewer)["count"] == 5
    client_api._do_read_notifications(viewer, mark=True)
    assert _badge(viewer)["count"] == 0
    assert not any(n["unread"] for n in _list(rid, viewer))


# ── 2. Urgent rows resolve ───────────────────────────────────────────────────

def test_an_opened_critical_low_stops_needing_you(db_path):
    """The audit's probe: record critical_low, open it -> still urgent:1."""
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    notify.record_notification(rid, "critical_low", db_path=db_path)
    row = _list(rid, viewer)[0]
    assert row["urgent"] and not row["resolved"] and row["resolves_on_open"]
    assert _badge(viewer)["urgent"] == 1
    _open(db_path, rid, viewer, row)
    row = _list(rid, viewer)[0]
    assert row["resolved"]
    assert _badge(viewer)["urgent"] == 0


def test_a_newer_critical_low_supersedes_the_older(db_path):
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    old = _alert(db_path, rid, "critical_low", fired_at="2026-09-20 09:00:00")
    new = _alert(db_path, rid, "critical_low", fired_at="2026-09-21 09:00:00")
    rows = {n["id"]: n for n in _list(rid, viewer)}
    assert rows[old]["resolved"] and not rows[old]["resolves_on_open"]
    assert not rows[new]["resolved"]
    assert _badge(viewer)["urgent"] == 1


def test_an_issue_row_resolves_with_its_issue(db_path):
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    issue, _ = issues.create_issue(rid, "manual", "Walk-in is warm", notify=False, db_path=db_path)
    row = _list(rid, viewer)[0]
    assert row["type"] == "issue" and row["urgent"]
    assert not row["resolved"] and not row["resolves_on_open"], "an issue's own status is the rule"
    # Opening it is not handling it: the issue is still open.
    _open(db_path, rid, viewer, row)
    assert _badge(viewer)["urgent"] == 1
    issues.resolve(rid, issue["id"], db_path=db_path)
    assert _list(rid, viewer)[0]["resolved"]
    assert _badge(viewer)["urgent"] == 0


def test_a_ref_row_resolves_once_no_longer_pending(db_path):
    rid = _restaurant(db_path)
    viewer = _user(db_path, rid)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO delayed_actions (restaurant_id, kind, label, payload_json, execute_at, status) "
                       "VALUES (?,?,?,?,datetime('now','+1 hour'),'pending')", (rid, "order_send", "Order", "{}"))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    alert = notify.record_notification(rid, "order_send_pending", db_path=db_path,
                                       ref_kind="delayed_action", ref_id=aid)
    assert not {n["id"]: n for n in _list(rid, viewer)}[alert]["resolved"]
    conn = get_conn(db_path)
    conn.execute("UPDATE delayed_actions SET status='sent' WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    row = {n["id"]: n for n in _list(rid, viewer)}[alert]
    assert row["resolved"] and row["can_undo"] is False


def test_another_locations_ref_is_read_for_resolved_but_never_acted_on(db_path):
    """The group list reads each row's subject at its own location; Undo
    stays with the location the session is on."""
    a = create_restaurant(Restaurant(name="Syrup", owner_email="o@x.test", location_group="Syrup",
                                     location_name="Wicker Park"), db_path=db_path)
    b = create_restaurant(Restaurant(name="Syrup", owner_email="o@x.test", location_group="Syrup",
                                     location_name="Logan Square"), db_path=db_path)
    conn = get_conn(db_path)
    live = conn.execute("INSERT INTO delayed_actions (restaurant_id, kind, label, payload_json, execute_at, status) "
                        "VALUES (?,?,?,?,datetime('now','+1 hour'),'pending')", (b, "order_send", "O", "{}")).lastrowid
    done = conn.execute("INSERT INTO delayed_actions (restaurant_id, kind, label, payload_json, execute_at, status) "
                        "VALUES (?,?,?,?,datetime('now','-1 hour'),'sent')", (b, "order_send", "O", "{}")).lastrowid
    conn.commit()
    conn.close()
    r_live = notify.record_notification(b, "order_send_pending", db_path=db_path, ref_kind="delayed_action", ref_id=live)
    r_done = notify.record_notification(b, "order_send_pending", db_path=db_path, ref_kind="delayed_action", ref_id=done)
    # The rows as the group list reads them, the session on location a.
    conn = get_conn(db_path)
    raw = conn.execute("SELECT * FROM alert_log WHERE id IN (?,?)", (r_live, r_done)).fetchall()
    refs = client_api._notification_refs(conn, raw, a)
    conn.close()
    assert refs[("delayed_action", live)]["pending"] and not refs[("delayed_action", done)]["pending"]
    by = {r["id"]: r for r in raw}
    assert client_api._notification_resolution(by[r_done], refs, set()) is True
    assert client_api._notification_resolution(by[r_live], refs, set()) is False
    viewer = {"id": 7, "role": "owner", "is_admin": 0}
    assert client_api._notification_ref_fields(by[r_live], refs, viewer, a)["can_undo"] is False


# ── 3. _log_alert keeps the ref unless the column is missing ─────────────────

class _Conn:
    def __init__(self, error):
        self.error, self.calls = error, []

    def execute(self, sql, args):
        self.calls.append(sql)
        if "ref_kind" in sql:
            raise self.error
        return type("C", (), {"lastrowid": 1})()

    def commit(self):
        pass

    def close(self):
        pass


def test_log_alert_drops_the_ref_only_for_a_missing_column(monkeypatch):
    missing = _Conn(sqlite3.OperationalError("table alert_log has no column named ref_kind"))
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: missing)
    assert notify._log_alert(1, "order_send_pending", ref_kind="delayed_action", ref_id=3) == 1
    assert len(missing.calls) == 2 and "ref_kind" not in missing.calls[1]

    locked = _Conn(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: locked)
    with pytest.raises(sqlite3.OperationalError):
        notify._log_alert(1, "order_send_pending", ref_kind="delayed_action", ref_id=3)
    assert len(locked.calls) == 1, "any other failure used to write the row without its ref"


# ── The web bell follows the same rules ──────────────────────────────────────

def test_the_web_bell_flips_resolved_on_open_and_offers_mark_all_on_the_same_count():
    src = open("templates/dashboard.html", encoding="utf-8").read()
    i = src.index("// ── In-app notifications")
    bell = src[i:src.index("window.toggleNotifPanel", i)]
    # Opening a row with no knowable subject handles it, as the server counts.
    noted = bell[bell.index("function _noteOpened(n)"):bell.index("function markOpened(n)")]
    assert "if (n.resolves_on_open) n.resolved = true;" in noted
    assert bell.count("_noteOpened(n)") >= 2, "both open paths (here, and another location) go through it"
    # "Mark all read" rides the list's unread count, which the server's badge
    # count now equals (the same rows, the same rule).
    assert "var foot = _unreadCount() ?" in bell
