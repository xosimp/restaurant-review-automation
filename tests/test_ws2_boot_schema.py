"""Release-safety fix round 10/26/26, WS2 — one boot schema (A05-07, R20),
run-once data migrations in data_migrations (A05-08) and the build-time reference schema
(A05-09).

models.boot_schema() is the whole boot DDL chain; hosted_dashboard,
worker.py, status_manager's reference schema and the suite's template all
build through it, so /health checks the auth, webhook, guest-marketing,
push and telemetry tables too, and their migrations fail the boot instead
of being swallowed."""
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3

import pytest

import models
import status_manager


@pytest.fixture(scope="module")
def reference():
    """The reference schema built from scratch once for this module."""
    return status_manager.build_reference_columns()


# ── /health's reference sees every boot table ────────────────────────────────

def test_reference_schema_includes_the_tables_init_db_never_built(reference):
    for table in ("users", "sessions", "memberships", "two_fa_challenges", "webhooks", "webhook_deliveries",
                  "guest_contacts", "guest_campaign_queue", "device_tokens", "sales_audits", "boot_events",
                  "data_migrations"):
        assert table in reference, table
    # ...with their newest columns.
    assert {"email_change_pending", "email_change_attempts", "policy_notice_dismissed"} <= reference["users"]
    assert "attempts" in reference["two_fa_challenges"]
    assert {"unlinked_at", "pos_pay_ok_by"} <= reference["memberships"]
    assert "event_id" in reference["webhook_deliveries"]


def test_a_missing_auth_column_fails_health(db_path, reference, monkeypatch):
    monkeypatch.setitem(status_manager._reference_schema, "cols", reference)
    status_manager._schema_ok.clear()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE users DROP COLUMN email_change_pending")
    conn.commit()
    conn.close()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 500 and payload["error"] == "schema_mismatch"


def test_build_reference_touches_only_its_scratch_file(monkeypatch, tmp_path):
    """A scratch boot must never reach the platform's own database (on
    Railway that is production's Postgres)."""
    seen = []
    real = sqlite3.connect

    def spy(path, *a, **k):
        seen.append(os.path.abspath(str(path)))
        return real(path, *a, **k)
    models.close_pooled_connections()
    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(models, "_pg_url_for", lambda p: None)
    status_manager.build_reference_columns()
    platform = os.path.abspath(models.DB_PATH)
    assert seen and platform not in seen
    assert len({os.path.basename(p) for p in seen}) == 1, set(seen)


# ── swallowed boot DDL now fails the boot ────────────────────────────────────

class _FailingAlters:
    """A connection whose ALTERs fail with `message`."""

    def __init__(self, conn, message):
        self._conn, self._message = conn, message

    def execute(self, sql, *a):
        if sql.lstrip().upper().startswith("ALTER"):
            raise sqlite3.OperationalError(self._message)
        return self._conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.mark.parametrize("module_name, init", [("webhooks", "init_webhooks"),
                                                ("guest_marketing", "init_guest_marketing")])
def test_init_ddl_failures_raise_and_already_applied_does_not(db_path, monkeypatch, module_name, init):
    import importlib
    mod = importlib.import_module(module_name)
    real = mod.get_conn
    monkeypatch.setattr(mod, "get_conn", lambda p=None: _FailingAlters(real(p), "duplicate column name: x"))
    getattr(mod, init)(db_path)                              # already applied: fine
    monkeypatch.setattr(mod, "get_conn", lambda p=None: _FailingAlters(real(p), "database is locked"))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        getattr(mod, init)(db_path)


def test_auth_alters_raise_unless_already_applied(db_path, monkeypatch):
    assert models.add_missing_columns(db_path, ["ALTER TABLE users ADD COLUMN role TEXT"]) == 0
    assert models.add_missing_columns(db_path, ["ALTER TABLE users ADD COLUMN ws2_probe TEXT"]) == 1
    real = models.boot_conn
    monkeypatch.setattr(models, "boot_conn", lambda p=None: _FailingAlters(real(p), "database is locked"))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        models.add_missing_columns(db_path, ["ALTER TABLE users ADD COLUMN ws2_other TEXT"])


# ── run-once data migrations ─────────────────────────────────────────────────

# Every-boot rewrites that live outside models.py's ledger today, by module,
# each with WHY it still runs on every boot. A NEW every-boot UPDATE or
# DELETE fails this test: put it on the ledger (models.run_once /
# models.run_once_sql / a (name, sql) entry in init_db's list) or, if it truly
# must run on every boot, add it here with why. The list may only shrink
# (RECURRING_BOOT_WRITES_CEILING). Blind re-audit 10/27/26 (RF6, B5 P3)
# moved the hot-table ones to the ledger — sessions, users, login_history
# (the nightly retention prune alone), ai_usage, the legacy 2FA scrub,
# demand_signals, market_events, capability_changes, email_suppressions,
# membership_pin_attempts — after checking that no writer produces their
# legacy shape any more. What is left, each a candidate for future cleanup
# after additional verification of its writers:
RECURRING_BOOT_WRITES = {
    "UPDATE ask_cavnar_conversations SET user_id": "models._attribute_ownerless_ask_chats — a caller may still "
                                                   "save a login-less turn (user_id None); kept until every writer "
                                                   "requires a login",
    "UPDATE ask_cavnar_messages SET user_id": "models._attribute_ownerless_ask_chats — as above",
    "UPDATE ask_topics SET user_id": "models._attribute_ownerless_ask_chats — as above",
    "UPDATE fintech_invoice_lines SET restaurant_id": "fintech_invoices.init — small table; writer not yet verified",
    "UPDATE guest_contacts SET source='toast_invite'": "guest_marketing.init_guest_marketing — the invite job's "
                                                      "writer not yet verified to stamp source on every path",
    "UPDATE guest_newsletter_recipients SET": "guest_marketing.init_guest_marketing — the send path's failure "
                                              "classification not yet verified to cover every legacy shape",
    "UPDATE labor_history SET kind='rolling_window'": "models — legacy labor windows; writer not yet verified",
    "UPDATE rec_instances SET silenced_until": "rec_ledger.init_rec_ledger — ONGOING: re-applies the current "
                                               "RECURRING_*_DAYS caps when they change; a row inside its cap is "
                                               "untouched",
    "UPDATE restaurants SET contract_signed_at": "billing_jobs.init_billing — small table; the signing path's "
                                                "own stamp not yet verified",
    "UPDATE restaurants SET converted_at": "billing_jobs.init_billing — as above, the first-payment stamp",
    "UPDATE schedule_shares SET expires_at=datetime(COALESCE(sent": "models.migrate_schedule_shares — share "
                                                                   "expiry backfill; writer not yet verified",
}
# The ratchet: this number only ever goes down.
RECURRING_BOOT_WRITES_CEILING = 11

# What init_db put on the ledger (A05-08): none of these may run again.
LEDGERED = ("temp_password", "marketing_content_log SET reach=NULL", "DELETE FROM recipe_ingredients",
            "labor_daily_history SET sales=NULL",
            # blind re-audit 10/27/26 (RF6): moved off every boot
            "UPDATE sessions SET", "DELETE FROM sessions WHERE device_type='admin-view-as'",
            "UPDATE users SET role='employee'", "UPDATE users SET username = LOWER(username)",
            "UPDATE restaurants SET two_fa_code=NULL", "DELETE FROM login_history", "UPDATE ai_usage SET",
            "DELETE FROM membership_pin_attempts", "DELETE FROM market_events", "demand_signals SET kind",
            "DELETE FROM demand_signals", "UPDATE capability_changes SET attribute",
            "UPDATE email_suppressions SET scope")


def _traced_boot(path, monkeypatch):
    """Run boot_schema on `path`; the UPDATE/DELETE statements it executed."""
    models.close_pooled_connections()
    ran = []
    real = sqlite3.connect

    def traced(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(lambda s: ran.append(re.sub(r"\s+", " ", s.strip()))
                             if s.lstrip().upper().startswith(("UPDATE", "DELETE")) else None)
        return c
    monkeypatch.setattr(sqlite3, "connect", traced)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            models.boot_schema(path)
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)
        models.close_pooled_connections()
    return ran


@pytest.mark.sqlite_only     # sqlite3's trace callback; the Postgres twin is test_ws2_pg_boot.py's WRITE_HOOKS test
def test_a_second_boot_runs_no_unrecorded_rewrite(db_path, monkeypatch):
    """db_path is a copy of a booted template: booting it is a second boot."""
    names = {m["name"] for m in models.applied_migrations(db_path)}
    assert {"temp_password_scrub_a3", "marketing_unmeasured_null_mb7", "recipe_pair_dedup",
            "seeded_targets_rule_v1", "missing_sales_null_ca3_f3"} <= names
    assert set(RF6_RUN_ONCE) <= names, set(RF6_RUN_ONCE) - names
    ran = _traced_boot(db_path, monkeypatch)
    for stmt in ran:
        assert not any(k in stmt for k in LEDGERED), f"a ledgered rewrite ran again: {stmt[:120]}"
        assert any(stmt.startswith(p) for p in RECURRING_BOOT_WRITES), \
            f"a new every-boot data rewrite: {stmt[:120]} — put it on models.run_once"


# The run-once names blind re-audit 10/27/26 (RF6) gave the rewrites it moved.
RF6_RUN_ONCE = ("pin_identity_role_v1", "usernames_lowercased_v1", "legacy_two_fa_scrub",
                "session_rows_normalised_v1", "pin_attempt_orphans_swept_v1", "ai_usage_vendor_outcome_v1",
                "ai_usage_price_version_v1", "market_events_churn_retracted_v1", "post_signals_migrated_v1",
                "capability_rating_subject_split_v1", "email_suppression_scope_all_v1")


def test_the_recurring_boot_writes_only_shrink():
    assert len(RECURRING_BOOT_WRITES) <= RECURRING_BOOT_WRITES_CEILING, (
        "a new every-boot data rewrite: put it on models.run_once / run_once_sql instead")


def test_moved_rewrites_run_on_an_upgraded_database_and_never_again(db_path):
    """The legacy shapes the moved rewrites fix, on a database that never
    recorded them (an upgrade from before this round): fixed on the first
    boot, recorded, and a row written in the legacy shape afterwards is left
    alone by the next boot (it is not the boot's business any more)."""
    conn = sqlite3.connect(db_path)
    rid = conn.execute("INSERT INTO restaurants (name, owner_email, two_fa_code) VALUES ('A','a@x.test','123456')"
                       ).lastrowid
    uid = conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, role) "
                       "VALUES (?, 'MixedCase', 'pin1@staff.invalid', 'x', 'client')", (rid,)).lastrowid
    conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES ('tok-rf6', ?, '2030-01-01T10:00:00+00:00')",
                 (uid,))
    conn.execute("INSERT INTO email_suppressions (email, reason, scope) VALUES ('s@x.test', 'bounce', NULL)")
    conn.execute(f"DELETE FROM data_migrations WHERE name IN ({','.join('?' * len(RF6_RUN_ONCE))})", RF6_RUN_ONCE)
    conn.commit()
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT two_fa_code FROM restaurants WHERE id=?", (rid,)).fetchone()[0] is None
        assert conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone() == ("mixedcase",
                                                                                                  "employee")
        assert conn.execute("SELECT expires_at FROM sessions WHERE token='tok-rf6'").fetchone()[0] == \
            "2030-01-01 10:00:00"
        assert conn.execute("SELECT scope FROM email_suppressions WHERE email='s@x.test'").fetchone()[0] == "all"
        names = {r[0] for r in conn.execute("SELECT name FROM data_migrations")}
        assert set(RF6_RUN_ONCE) <= names, set(RF6_RUN_ONCE) - names
        conn.execute("UPDATE users SET username='AgainMixed' WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()[0] == "AgainMixed"
    finally:
        conn.close()


def test_retired_secret_columns_are_refused_at_write_time(db_path, caplog):
    rid = models.create_restaurant(models.Restaurant(name="R", owner_email="r@x.test", temp_password="pw-1"),
                                   db_path=db_path)
    assert models.get_restaurant(rid, db_path=db_path).temp_password in (None, "")
    with caplog.at_level("WARNING", logger="models"):
        models.update_restaurant(rid, {"temp_password": "pw-2", "two_fa_code": "123456",
                                       "two_fa_pending": "secret", "name": "Renamed"}, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.name == "Renamed"
    assert not r.temp_password and not r.two_fa_code and not r.two_fa_pending
    assert "refused retired secret column" in caplog.text


def test_a_ledgered_rewrite_runs_on_a_database_that_never_had_it(db_path):
    conn = sqlite3.connect(db_path)
    rid = conn.execute("INSERT INTO restaurants (name, owner_email, temp_password) VALUES ('A','a@x.test','pw')"
                       ).lastrowid
    conn.execute("DELETE FROM data_migrations WHERE name='temp_password_scrub_a3'")
    conn.commit()
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT temp_password FROM restaurants WHERE id=?", (rid,)).fetchone()[0] is None
    row = conn.execute("SELECT detail FROM data_migrations WHERE name='temp_password_scrub_a3'").fetchone()
    assert row and row[0].startswith("rows=1")
    # ...and once recorded, a row written since is left alone.
    conn.execute("UPDATE restaurants SET temp_password='kept' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT temp_password FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "kept"
    conn.close()


def test_run_once_records_only_success(db_path):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("no")
    assert models.run_once("ws2_probe", boom, db_path=db_path) is False
    assert "ws2_probe" not in {m["name"] for m in models.applied_migrations(db_path)}
    assert models.run_once("ws2_probe", lambda: calls.append(2) or 7, db_path=db_path) is True
    assert models.run_once("ws2_probe", lambda: calls.append(3), db_path=db_path) is False
    assert calls == [1, 2]
    row = [m for m in models.applied_migrations(db_path) if m["name"] == "ws2_probe"][0]
    assert row["detail"].startswith("rows=7")


def test_missing_sales_rewrite_no_longer_nulls_a_new_day_on_the_next_boot(db_path):
    """The audit's wrong-data case: labor written before its sales arrived."""
    rid = models.create_restaurant(models.Restaurant(name="R", owner_email="r@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost, labor_pct) "
                 "VALUES (?, '2026-10-20', 0, 500, 0)", (rid,))
    conn.commit()
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT sales FROM labor_daily_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    conn.close()


# ── the build-time reference schema (A05-09) ─────────────────────────────────

def test_artifact_is_used_when_built_from_this_code(tmp_path, monkeypatch, reference):
    path = str(tmp_path / "reference_schema.json")
    status_manager.write_reference_artifact(path=path, cols=reference)
    monkeypatch.setenv("REFERENCE_SCHEMA_PATH", path)
    monkeypatch.setattr(status_manager, "_reference_schema", {})

    def no_boot(*a, **k):
        raise AssertionError("built the reference although the image carries it")
    monkeypatch.setattr(status_manager, "build_reference_columns", no_boot)
    cols = status_manager._reference_columns()
    assert cols == reference and status_manager._reference_schema["source"] == "artifact"


def test_artifact_from_other_code_is_ignored(tmp_path, monkeypatch, reference):
    path = str(tmp_path / "reference_schema.json")
    status_manager.write_reference_artifact(path=path, cols=reference)
    body = json.load(open(path))
    body["code_hash"] = "0" * 64
    json.dump(body, open(path, "w"))
    assert status_manager.load_reference_artifact(path) is None
    monkeypatch.setenv("REFERENCE_SCHEMA_PATH", path)
    monkeypatch.setattr(status_manager, "_reference_schema", {})
    monkeypatch.setattr(status_manager, "build_reference_columns", lambda: {"t": {"c"}})
    assert status_manager._reference_columns() == {"t": {"c"}}
    assert status_manager._reference_schema["source"] == "built"
    assert status_manager.load_reference_artifact(str(tmp_path / "absent.json")) is None


def test_the_build_script_scrubs_production_from_its_environment(tmp_path):
    from scripts import build_reference_schema as brs
    env = {"DATABASE_URL": "postgresql://prod/db", "RESEND_API_KEY": "re_x", "STRIPE_SECRET_KEY": "sk_live_x",
           "TWILIO_AUTH_TOKEN": "t", "CREDENTIAL_KEY": "k", "PATH": "/bin"}
    out = brs.scrub_environment(env, volume=str(tmp_path))
    assert set(out) == {"PATH", "DB_BACKEND", "RUN_SCHEDULER_IN_WEB", "RAILWAY_VOLUME_MOUNT_PATH"}
    assert out["DB_BACKEND"] == "sqlite" and out["RAILWAY_VOLUME_MOUNT_PATH"] == str(tmp_path)


def test_nixpacks_builds_the_artifact_and_never_fails_the_build():
    import tomllib
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "nixpacks.toml"), "rb") as f:
        cfg = tomllib.load(f)
    cmds = cfg["phases"]["build"]["cmds"]
    assert any("scripts/build_reference_schema.py" in c and "|| echo" in c for c in cmds)
    # The install layer still sees only the lock (build speed, 10/15/26).
    assert cfg["phases"]["install"]["onlyIncludeFiles"] == ["requirements.txt"]
