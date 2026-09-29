"""Memory fix round (9/29/26), workstream M7 — "change_log".

Settings, targets, prices, menu, roster and hours changes left no lasting,
attributed history: target changes lived in activity_log (no actor column,
pruned at 180 days), any changed target read as "your target" whoever set
it, and a price typed on Food Cost or synced left no trace for the "changed
since" caution. change_log is one append-only table kept forever, written by
models.update_restaurant inside its own transaction and by every other
writer through record(); rec_trust.owner_changes and outcomes.find_concurrent
read it.
"""
import ast
import inspect
import json
import sqlite3

import pytest
from flask import Flask, g

import change_log
import models
import thresholds
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


_app = Flask(__name__)


def _rid(name="Change Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", **kw))


def _log(rid, **where):
    sql = "SELECT * FROM change_log WHERE restaurant_id=?"
    args = [rid]
    for k, v in where.items():
        sql += f" AND {k}=?"
        args.append(v)
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]
    finally:
        conn.close()


def _as(user):
    """A request whose auth decorator resolved `user` — the same call every
    decorator makes (auth._bind_log_context)."""
    import auth
    ctx = _app.test_request_context("/api/account/targets", method="POST")
    ctx.push()
    auth._bind_log_context(user)
    return ctx


OWNER = {"id": 11, "role": "owner", "is_admin": 0}
MANAGER = {"id": 12, "role": "manager", "is_admin": 0}
ADMIN = {"id": 1, "role": "client", "is_admin": 1}


# ── the classification every update_restaurant field must have ─────────────

def _allowed():
    src = inspect.getsource(models.update_restaurant)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "allowed":
            return ast.literal_eval(node.value)
    raise AssertionError("update_restaurant's allowed set not found")


def test_every_restaurant_field_is_recorded_or_deliberately_not():
    allowed = _allowed()
    tracked, untracked = set(change_log.RESTAURANT_FIELD_KINDS), change_log.RESTAURANT_UNTRACKED
    assert not (tracked & untracked)
    assert allowed - tracked - untracked == set(), "a restaurants field nobody classified for the change log"
    import credentials
    assert not (tracked & set(credentials.FIELDS)), "a credential would be written into the change log"
    assert not (tracked & set(models.BILLING_HISTORY_FIELDS)), "billing has its own history"
    for f in ("labor_target_pct", "never_say", "open_times_json", "auto_publish_schedule", "hourly_rate"):
        assert f in tracked, f


# ── update_restaurant writes it, in its transaction ─────────────────────────

def test_a_target_change_is_recorded_with_its_old_and_new_value():
    rid = _rid()
    update_restaurant(rid, {"labor_target_pct": 28.0})
    rows = _log(rid, field="labor_target_pct")
    assert len(rows) == 1
    r = rows[0]
    assert (r["kind"], r["entity"], json.loads(r["old_value"]), json.loads(r["new_value"])) == \
        ("target", "restaurant", 30.0, 28.0)
    assert r["source"] == "system" and r["actor_user_id"] is None


def test_an_unchanged_value_an_untracked_field_and_a_credential_record_nothing():
    rid = _rid()
    update_restaurant(rid, {"labor_target_pct": 30.0, "gbp_rating": 4.5, "toast_client_secret": "s3cret",
                            "last_active_tab": "labor"})
    assert _log(rid) == []
    conn = models.get_conn()
    try:
        blob = " ".join(str(v) for r in conn.execute("SELECT * FROM change_log").fetchall() for v in tuple(r))
    finally:
        conn.close()
    assert "s3cret" not in blob


def test_a_stale_write_records_nothing():
    rid = _rid()
    v = models.restaurant_version(rid)
    update_restaurant(rid, {"labor_target_pct": 27.0})
    with pytest.raises(models.StaleWrite):
        update_restaurant(rid, {"labor_target_pct": 25.0}, expected_version=v)
    assert [json.loads(r["new_value"]) for r in _log(rid, field="labor_target_pct")] == [27.0]


@pytest.mark.parametrize("user,source,actor,role", [
    (OWNER, "owner", 11, "owner"),
    (MANAGER, "manager", 12, "manager"),
    (ADMIN, "admin", 1, "admin"),
    (dict(OWNER, acting_admin_id=1), "admin", 1, "view-as"),
])
def test_who_made_the_change_is_the_login_the_request_resolved(user, source, actor, role):
    rid = _rid()
    ctx = _as(user)
    try:
        update_restaurant(rid, {"never_say": "cheap, deal"})
    finally:
        ctx.pop()
    r = _log(rid, field="never_say")[0]
    assert (r["kind"], r["source"], r["actor_user_id"], r["actor_role"]) == ("never_say", source, actor, role)
    assert r["via"].startswith("request:")


def test_a_manager_clearing_the_never_say_list_is_kept_with_what_it_said():
    rid = _rid(never_say="cheap, deal, fancy")
    ctx = _as(MANAGER)
    try:
        update_restaurant(rid, {"never_say": ""})
    finally:
        ctx.pop()
    r = _log(rid, field="never_say")[0]
    assert json.loads(r["old_value"]) == "cheap, deal, fancy" and r["source"] == "manager"
    import ops
    assert "change_log" not in ops._RETENTION_DAYS, "kept forever"


def test_a_seeded_target_is_recorded_as_seeded_not_as_whoever_asked():
    rid = _rid()
    ctx = _as(ADMIN)
    try:
        update_restaurant(rid, {"labor_target_pct": 31.5, "labor_target_source": "seeded"})
    finally:
        ctx.pop()
    assert _log(rid, field="labor_target_pct")[0]["source"] == "seeded"


def test_attributed_names_the_source_off_a_request():
    rid = _rid()
    with change_log.attributed(source="sync", via="pos:toast"):
        update_restaurant(rid, {"open_times_json": json.dumps({"Monday": "11:00"})})
    r = _log(rid, kind="hours")[0]
    assert (r["source"], r["via"]) == ("sync", "pos:toast")


def test_a_log_that_cannot_be_written_never_fails_the_setting_and_is_reported_after(monkeypatch):
    rid = _rid()
    conn = models.get_conn()
    conn.execute("DROP TABLE change_log")
    conn.commit()
    conn.close()
    reported = []
    monkeypatch.setattr(change_log, "report", lambda e, *a, **k: reported.append(str(e)))
    update_restaurant(rid, {"labor_target_pct": 26.0})
    assert models.get_restaurant(rid).labor_target_pct == 26.0
    assert reported and "change_log" in reported[0]


# ── "your target" only when a principal set it ──────────────────────────────

@pytest.mark.parametrize("user,setter,label,phrase", [
    (OWNER, "principal", "your target", "your 28% target"),
    (ADMIN, "admin", "the target Cavnar AI set", "the 28% target Cavnar AI set"),
    (MANAGER, "delegate", "the target a manager set", "the 28% target a manager set"),
])
def test_a_target_says_your_only_when_an_account_holder_set_it(user, setter, label, phrase):
    rid = _rid()
    ctx = _as(user)
    try:
        update_restaurant(rid, {"labor_target_pct": 28.0})
    finally:
        ctx.pop()
    r = models.get_restaurant(rid)
    assert r.labor_target_source == "set"
    assert json.loads(r.target_setters_json) == {"labor_target_pct": setter}
    t = thresholds.target_for(r, "labor")
    assert (t["label"], t["phrase"], t["setter"]) == (label, phrase, setter)


def test_a_target_set_before_setters_were_recorded_still_reads_as_yours():
    rid = _rid()
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET labor_target_pct=27, labor_target_source='set' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    assert thresholds.target_for(models.get_restaurant(rid), "labor")["label"] == "your target"


def test_the_admin_form_that_names_the_source_still_records_the_admin_as_setter():
    rid = _rid()
    ctx = _as(ADMIN)
    try:
        update_restaurant(rid, {"food_cost_target": 29.0, "food_cost_target_source": "set"})
    finally:
        ctx.pop()
    assert thresholds.target_label(models.get_restaurant(rid), "food") == "the target Cavnar AI set"


def test_the_owners_targets_card_records_the_owner(monkeypatch):
    import strategy_routes as sr
    rid = _rid()
    u = {"id": 11, "restaurant_id": rid, "role": "owner"}
    monkeypatch.setattr(sr, "_body", lambda: {"labor_target_pct": 27})
    ctx = _as(u)
    try:
        out, code = sr._do_targets_set(u)
    finally:
        ctx.pop()
    assert code == 200
    r = _log(rid, field="labor_target_pct")[0]
    assert (r["source"], r["actor_user_id"]) == ("owner", 11)
    assert thresholds.target_label(models.get_restaurant(rid), "labor") == "your target"


# ── the history answers "which target applied then?" ─────────────────────────

def test_value_as_of_answers_which_target_applied_on_a_date():
    rid = _rid()
    conn = models.get_conn()
    for old, new, at in ((30, 28, "2026-03-10 15:00:00"), (28, 26, "2026-06-01 09:00:00")):
        conn.execute("INSERT INTO change_log (restaurant_id, kind, entity, field, old_value, new_value, source, "
                     "changed_at) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, "target", "restaurant", "labor_target_pct", json.dumps(old), json.dumps(new), "owner", at))
    conn.commit()
    conn.close()
    assert change_log.value_as_of(rid, "labor_target_pct", "2026-02-01")["value"] == 30
    assert change_log.value_as_of(rid, "labor_target_pct", "2026-03-10")["value"] == 28
    assert change_log.value_as_of(rid, "labor_target_pct", "2026-04-15")["value"] == 28
    assert change_log.value_as_of(rid, "labor_target_pct", "2026-09-01")["value"] == 26
    assert change_log.value_as_of(rid, "food_cost_target", "2026-09-01", current=31)["value"] == 31
    hist = change_log.history(rid, kinds="target")
    assert [json.loads(json.dumps(h["new_value"])) for h in hist] == [26, 28]
    line = change_log.describe(hist[0])
    assert line == "Labor target pct: 28 → 26, by the owner on 6/1/26"


def test_record_names_the_thing_changed_and_derives_its_kind():
    rid = _rid()
    change_log.record(rid, "menu_item", "sell_price", 14.0, 15.5, subject="Salmon", source="sync")
    change_log.record(rid, "roster:Maria G.", "left", True, False, source="manager")
    change_log.record(rid, "menu_item", "added", None, "Burrata", subject="Burrata")
    kinds = {(r["kind"], r["subject"], r["source"]) for r in _log(rid)}
    assert kinds == {("price", "Salmon", "sync"), ("roster_leave", "Maria G.", "manager"),
                     ("menu_add", "Burrata", "system")}


def test_record_never_raises():
    assert change_log.record(10**9, "restaurant", "labor_target_pct", 30, 28) is None     # no such restaurant
    assert change_log.record(1, "restaurant", "labor_target_pct", 30, 30) is None         # no change


# ── the readers: the "changed since" caution and the concurrent check ──────

def test_owner_changes_reads_the_log_and_says_who(monkeypatch):
    import rec_trust
    rid = _rid()
    change_log.record(rid, "menu_item", "sell_price", 14.0, 15.5, subject="Salmon", source="manager")
    change_log.record(rid, "menu_item", "sell_price", 9.0, 9.5, subject="Wings", source="sync")
    change_log.record(rid, "restaurant", "labor_target_pct", 30, 28, source="owner")
    out = rec_trust.owner_changes(rid)
    by = {c["change_kind"]: c for c in out if c.get("change_kind")}
    assert by["price"]["what"] == "changed 2 prices" and by["price"]["who"] == "A sync"
    assert set(by["price"]["sources"]) == {"inventory", "sales"}
    assert by["target"]["what"] == "changed your labor target" and by["target"]["who"] == "You"

    class Ctx:
        rid = 0

        def changes(self):
            return out
    got = rec_trust.changed_since(Ctx(), "reprice:salmon",
                                  [{"key": "inventory", "pct": 90, "as_of_iso": "2020-01-01"}])
    assert got["caution"].startswith("A sync changed 2 prices on ")


def test_a_price_synced_or_a_person_leaving_is_a_concurrent_change():
    import outcomes
    rid = _rid()
    conn = models.get_conn()
    for kind, field, subj, at in (("price", "sell_price", "Salmon", "2026-08-12 10:00:00"),
                                  ("roster_leave", "left", "Maria G.", "2026-08-14 10:00:00"),
                                  ("target", "labor_target_pct", None, "2026-08-13 10:00:00")):
        conn.execute("INSERT INTO change_log (restaurant_id, kind, entity, field, subject, source, changed_at) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, kind, "x", field, subj, "sync", at))
    conn.commit()
    conn.close()
    food = outcomes.find_concurrent({"id": 0, "restaurant_id": rid, "metric": "food_cost_pct"},
                                    "2026-08-01", "2026-08-31")
    assert {"kind": "price_change", "label": "Salmon repriced", "date": "2026-08-12"} in food
    assert not [c for c in food if "Maria" in c["label"]]
    labor = outcomes.find_concurrent({"id": 0, "restaurant_id": rid, "metric": "labor_pct"},
                                     "2026-08-01", "2026-08-31")
    assert {"kind": "roster_leave_change", "label": "Maria G. left the team", "date": "2026-08-14"} in labor
    assert not [c for c in labor if "target" in c["label"].lower()], "a target moves no number"


# ── the carry-over from activity_log ────────────────────────────────────────

def test_activity_logs_target_and_profile_changes_are_carried_over_once():
    rid = _rid()
    conn = models.get_conn()
    conn.execute("DELETE FROM data_migrations WHERE name=?", (change_log.BACKFILL_MIGRATION,))
    conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) VALUES (?,?,?,?)",
                 (rid, "target_change", json.dumps({"field": "labor_target_pct", "from": 30, "to": 28}),
                  "2026-09-11T15:15:05"))
    conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data) VALUES (?,?,?)",
                 (rid, "profile_changed", json.dumps({"concept": {"from": None, "to": "italian"}})))
    conn.commit()
    assert change_log._backfill_from_activity_log(conn) == 2
    assert change_log._backfill_from_activity_log(conn) == 0
    conn.close()
    rows = _log(rid)
    assert {(r["field"], r["source"], r["kind"]) for r in rows} == {("labor_target_pct", "legacy", "target"),
                                                                    ("concept", "legacy", "profile")}
    assert [r["changed_at"] for r in rows if r["field"] == "labor_target_pct"] == ["2026-09-11 15:15:05"]


def test_the_table_is_made_at_boot_and_the_decorators_keep_the_login_on_g():
    import auth
    assert "init_change_log" in inspect.getsource(models.init_db)
    with _app.test_request_context("/"):
        auth._bind_log_context(OWNER)
        assert g.cavnar_current_user is OWNER
    for dec in (auth.login_required, auth.admin_required, auth.mobile_login_required):
        assert "_bind_log_context(" in inspect.getsource(dec)
