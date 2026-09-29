"""The controls docs/ops/SECURITY.md, RECOVERY.md and RAILWAY_SCHEDULER_SPLIT.md
state, checked against the code (fix round, #146).

An operator reads those files during an incident and acts on them. Each test
below pairs one stated control with the code that implements it — reading the
source or calling the function — and with the sentence in the doc that states
it, so a change to either side fails here until the other is brought along.
When a test fails because the code changed on purpose, update the doc and the
expected value together; when it fails because the doc changed, check the code
still does what the new sentence says.
"""
import inspect
import json
import os
import re
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _doc(rel):
    """The doc as one lower-case line: sentences wrap anywhere."""
    return " ".join(_read(rel).split()).lower()


SECURITY = "docs/ops/SECURITY.md"
RECOVERY = "docs/ops/RECOVERY.md"
SPLIT = "docs/ops/RAILWAY_SCHEDULER_SPLIT.md"


def _says(rel, *phrases):
    text = _doc(rel)
    for p in phrases:
        assert " ".join(p.split()).lower() in text, f"{rel} no longer says: {p!r}"


# ── sessions ────────────────────────────────────────────────────────────────

def test_admin_and_support_sessions_last_twelve_hours_absolute():
    import auth
    assert auth.ADMIN_SESSION_HOURS == 12
    _says(SECURITY, "admin and support: 12 hours absolute")
    _says(RECOVERY, "admin sessions last 12 hours")


def test_a_view_as_session_lasts_two_hours_absolute():
    import auth
    assert auth.VIEW_AS_HOURS == 2
    src = inspect.getsource(auth.create_view_as_session)
    assert "VIEW_AS_HOURS" in src
    _says(SECURITY, "view-as: 2 hours absolute")


def test_owner_web_and_staff_session_lifetimes():
    import auth
    assert inspect.signature(auth.create_session).parameters["days"].default == 30
    assert auth.INACTIVITY_HOURS == 8
    assert auth.STAFF_SESSION_HOURS == 14
    assert set(auth._INACTIVITY_EXEMPT_DEVICES) == {"ios", "staff_pin"}
    _says(SECURITY, "30 days absolute, and on the web an 8-hour inactivity rule",
          "staff pin sessions: 14 hours")


def test_session_tokens_are_stored_as_a_sha256():
    import hashlib
    import auth
    assert auth.hash_session_token("abc") == hashlib.sha256(b"abc").hexdigest()
    _says(SECURITY, "sha-256 at rest")


# ── step-up and the admin second factor ─────────────────────────────────────

def test_step_up_refuses_without_a_password_typed_in_fifteen_minutes():
    from flask import Flask
    import auth
    assert auth.RECENT_AUTH_MINUTES == 15

    @auth.recent_auth_required()
    def action(current_user=None):
        return "done"

    app = Flask(__name__)
    with app.test_request_context("/admin/api/x", method="POST"):
        resp, status = action(current_user={"id": 1, "is_admin": 1, "reauth_at": None})
        assert status == 403
        body = resp.get_json()
        assert body["reauth_required"] is True and body["reauth_url"] == "/admin/api/reauth"
        assert body["window_minutes"] == 15
        assert action(current_user={"id": 1, "is_admin": 1, "reauth_at": auth.sql_utc()}) == "done"
    _says(SECURITY, "the last **15 minutes**", "reauth_required: true")


# The routes SECURITY.md lists as carrying step-up today. When a route gains
# or loses @recent_auth_required, change this set and the doc's list together.
_STEP_UP_TODAY = {
    "/admin/deactivate-client/<int:user_id>",
    "/admin/api/set-user-role",
    "/admin/two-factor/disable",
    "/admin/two-factor/backup-codes",
    "/admin/api/me/username",
    "/admin/api/users/<int:user_id>/reset-2fa",
    "/admin/api/users/<int:user_id>/clear-lockout",
    "/admin/api/users/<int:user_id>/sessions/<session_id>/revoke",
    "/admin/api/users/<int:user_id>/revoke-sessions",
    "/admin/api/support-logins",
}


def test_the_step_up_routes_are_the_ones_the_doc_lists():
    import ast
    import glob
    found = set()
    files = ["admin_routes.py", "auth_routes.py", "client_api.py", "mobile_api.py", "strategy_routes.py"]
    files += [os.path.relpath(f, ROOT) for f in glob.glob(os.path.join(ROOT, "*_routes.py"))]
    for rel in sorted(set(files)):
        src = _read(rel)
        if "recent_auth_required" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.FunctionDef):
                continue
            names = [d.func.attr if isinstance(d.func, ast.Attribute) else getattr(d.func, "id", "")
                     for d in node.decorator_list if isinstance(d, ast.Call)]
            if "recent_auth_required" not in names:
                continue
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "route":
                    found.add(d.args[0].value)
    assert found == _STEP_UP_TODAY
    _says(SECURITY, "applied today", "not yet")


def test_the_admin_second_factor_states():
    import auth
    owner = {"id": 5, "is_admin": 0, "role": "client"}
    admin = {"id": 1, "is_admin": 1, "role": "client", "two_fa_enabled": 0}
    enrolled = dict(admin, two_fa_enabled=1)
    old = os.environ.pop("ADMIN_REQUIRE_2FA", None)
    try:
        assert auth.admin_second_factor_state(owner) == "ok"
        assert auth.admin_second_factor_state(admin) == "ok"
        os.environ["ADMIN_REQUIRE_2FA"] = "1"
        assert auth.admin_second_factor_state(admin) == "enrol"
        assert auth.admin_second_factor_state(enrolled) == "verify"
        assert auth.admin_second_factor_state(dict(enrolled, two_factor_at="2026-09-29 10:00:00")) == "ok"
    finally:
        os.environ.pop("ADMIN_REQUIRE_2FA", None)
        if old is not None:
            os.environ["ADMIN_REQUIRE_2FA"] = old
    _says(SECURITY, "`ok`; `enrol` (only when `admin_require_2fa=1`", "`verify` (a session from before enrolment is ended",
          "`error` (fails closed, 503)")


def test_support_may_write_only_view_as_and_its_own_enrolment():
    import auth
    assert set(auth._SUPPORT_WRITE_OK) == {"admin.view_as_client", "admin.admin_two_factor_send",
                                            "admin.admin_two_factor_verify"}
    _says(SECURITY, "except opening a view-as and its own two-factor enrolment")


# ── lockouts, the request ceiling, the password policy ─────────────────────

def test_login_lockout_thresholds():
    import security
    assert security.ACCOUNT_MAX == 5 and security.WINDOW_MINUTES == 15
    assert tuple(security.LOCK_STEPS) == ((5, 5), (10, 30), (15, 24 * 60))
    assert security.IP_MAX == 25 and security.IP_MAX_ANON == 5
    assert security.INTERNAL_DAY_MAX == 100 and security.INTERNAL_DAY_LOCK_MINUTES == 60
    src = inspect.getsource(security.login_throttled)
    assert "_acct_ip_key(username, ip)" in src and "known_device" in src
    _says(SECURITY, "5 failures in 15 minutes locks it", "an admin or support login is locked per address",
          "100 failures a day")


def test_the_admin_request_ceiling():
    import security
    assert security.ADMIN_REQUESTS_PER_MINUTE == 240 and security.ADMIN_WRITES_PER_MINUTE == 60
    security.reset_admin_rate_limits()
    try:
        key = "docs-controls-test"
        for i in range(60):
            assert security.admin_request_allowed(key, is_write=True, now=1000.0 + i * 0.01)[0]
        allowed, retry_after = security.admin_request_allowed(key, is_write=True, now=1001.0)
        assert not allowed and retry_after >= 1
    finally:
        security.reset_admin_rate_limits()
    _says(SECURITY, "240 requests and 60 writes a minute per admin session")


def test_the_password_policy_is_eight_characters_and_not_breached():
    import auth
    assert auth.PASSWORD_MIN_LENGTH == 8
    assert auth.password_policy_error("short7!")
    assert "password_pwned" in inspect.getsource(auth.password_policy_error)
    assert "generated" in inspect.signature(auth.create_user).parameters
    _says(SECURITY, "at least 8 characters and not in a known breach")


def test_the_welcome_carries_a_72_hour_set_password_link_not_a_password():
    import billing_jobs
    assert billing_jobs.WELCOME_LINK_HOURS == 72
    _says(SECURITY, "temporary passwords are never stored or emailed", "72 hours from signing")


# ── break-glass and the boot seed ───────────────────────────────────────────

def test_the_break_glass_variables_are_read_at_every_boot():
    import auth
    import security
    assert "LOGIN_UNLOCK_USERNAMES" in inspect.getsource(security.apply_boot_unlocks)
    assert "ADMIN_2FA_RESET_USERNAMES" in inspect.getsource(auth.apply_boot_two_factor_resets)
    boot = _read("hosted_dashboard.py")
    assert "apply_boot_unlocks()" in boot and "apply_boot_two_factor_resets" in boot
    assert os.path.exists(os.path.join(ROOT, "scripts", "unlock_login.py"))
    _says(SECURITY, "login_unlock_usernames", "admin_2fa_reset_usernames", "scripts/unlock_login.py")
    _says(RECOVERY, "login_unlock_usernames=<username>", "admin_2fa_reset_usernames=<username>")


def test_the_admin_seed_needs_a_password_and_no_admin(db_path):
    import auth
    auth.init_auth(db_path)
    refused = auth.ensure_admin_login(db_path=db_path, env={})
    assert refused["action"] == "refused" and "ADMIN_PASSWORD" in refused["reason"]
    created = auth.ensure_admin_login(db_path=db_path, env={"ADMIN_PASSWORD": "a-long-test-passphrase-9"})
    assert created["action"] == "created"
    again = auth.ensure_admin_login(db_path=db_path, env={"ADMIN_USERNAME": "someone-else",
                                                         "ADMIN_PASSWORD": "a-long-test-passphrase-9"})
    assert again["action"] == "exists"
    _says(SECURITY, "only when no admin login exists at all")


# ── credentials, share links, kept secrets ──────────────────────────────────

def test_every_encrypted_credential_column_is_named_in_the_doc():
    import credentials
    text = _read(SECURITY)
    missing = [f for f in credentials.FIELDS if f"`{f}`" not in text]
    assert missing == []


def test_plaintext_credentials_are_re_saved_encrypted_at_boot():
    boot = _read("hosted_dashboard.py")
    post = boot[boot.index("def _post_boot"):]
    assert "encrypt_existing()" in post
    _says(SECURITY, "at every boot `credentials.encrypt_existing()` re-saves any plaintext value encrypted")


def test_sales_audit_share_links_are_hashed_and_expire():
    import sales_audits
    assert sales_audits._hash_token("abc").startswith("sha256:")
    assert sales_audits.SHARE_TTL_DAYS == 90
    _says(SECURITY, "expire after 90 days")


def test_public_links_are_signed_with_kept_secrets():
    import emails
    import guest_links
    import issues
    assert guest_links._JOIN_SECRET == "join_links"
    assert emails._PAY_SECRET == "pay_links"
    assert 'kept_secret("issue_links")' in inspect.getsource(issues)
    _says(SECURITY, "`pay_links:vn`, `join_links:vn`, `issue_links:vn`")


# ── inbound webhooks ─────────────────────────────────────────────────────────

def test_the_stripe_webhook_fails_closed_without_a_secret(monkeypatch):
    from flask import Flask
    import webhook_routes
    seen = []
    monkeypatch.setattr(webhook_routes, "_webhook_seen", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(webhook_routes, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    resp = app.test_client().post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert resp.status_code == 401
    assert seen and seen[0][0][:2] == ("stripe", False)
    _says(SECURITY, "unset: every stripe webhook is refused **401** and counted")


def test_webhook_signature_failures_are_counted_per_provider():
    import models
    import webhook_routes
    assert "webhook_verifications" in inspect.getsource(webhook_routes._webhook_seen)
    assert "inbound_webhook_health" in inspect.getsource(models.record_inbound_webhook)
    _says(SECURITY, "every failure is counted per provider", "at most once an hour per provider")


# ── the backup ───────────────────────────────────────────────────────────────

def test_the_local_snapshot_is_never_scrubbed_and_the_offsite_copy_is():
    import scheduler
    src = inspect.getsource(scheduler.backup_db)
    assert "_redact_snapshot(redacted_path)" in src and "_redact_snapshot(local_path)" not in src
    assert "_encrypt_file_chunked(redacted_path, enc_path, key)" in src
    _says(SECURITY, "deliberately not redacted")
    _says(RECOVERY, "the local snapshots are complete and directly restorable")


def test_the_backup_fails_without_a_key_or_an_offsite_copy():
    import scheduler
    src = inspect.getsource(scheduler.backup_db)
    assert "BACKUP_ENCRYPTION_KEY is not set" in src
    assert "if not targets:" in src and "no off-site copy was made tonight" in src
    _says(SECURITY, "the nightly backup fails")
    _says(RECOVERY, "or no off-site copy was made")


def test_the_scrub_empties_the_tables_the_doc_names():
    import offsite_backup
    assert set(offsite_backup.SCRUB_TABLES) == {"sessions", "two_fa_backup_codes", "trusted_devices",
                                                "device_tokens", "login_reports", "staff_portal_tokens",
                                                "app_secrets", "view_as_sessions"}
    schema = _read("DATABASE_SCHEMA.md")
    assert all(f"`{t}`" in schema for t in offsite_backup.SCRUB_TABLES)


def test_an_unclassified_credential_column_is_scrubbed_anyway():
    import offsite_backup
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, vendor_api_key TEXT)")
        conn.execute("CREATE TABLE restaurants (id INTEGER PRIMARY KEY, gmb_refresh_token TEXT)")
        wipe, null, unclassified = offsite_backup.scrub_plan(conn)
    finally:
        conn.close()
    assert ("widgets", "vendor_api_key") in unclassified and ("widgets", "vendor_api_key") in null
    assert ("restaurants", "gmb_refresh_token") in null
    _says(SECURITY, "a credential-looking column in no list is scrubbed anyway")


def test_the_object_store_is_https_only(monkeypatch):
    import offsite_backup
    for k, v in {"BACKUP_S3_BUCKET": "b", "BACKUP_S3_ACCESS_KEY_ID": "k", "BACKUP_S3_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "http://store.example")
    assert offsite_backup.s3_config() is None
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://store.example")
    cfg = offsite_backup.s3_config()
    assert cfg["region"] == "auto" and cfg["prefix"] == "cavnar-backups/"
    _says(SECURITY, "`backup_s3_endpoint` (https)")
    _says(RECOVERY, "the prefix `cavnar-backups/` by default")


def test_an_offsite_copy_decrypts_line_by_line_with_the_key(tmp_path):
    from cryptography.fernet import Fernet
    import scheduler
    key = Fernet.generate_key().decode()
    src, enc, out = tmp_path / "a.db", tmp_path / "a.db.enc", tmp_path / "b.db"
    src.write_bytes(os.urandom(scheduler._BACKUP_CHUNK + 1000))
    scheduler._encrypt_file_chunked(str(src), str(enc), key)
    assert enc.read_bytes().count(b"\n") == 2
    scheduler._decrypt_file_chunked(str(enc), str(out), key)
    assert out.read_bytes() == src.read_bytes()
    _says(RECOVERY, "decrypt it line by line with the escrowed key", "check the sha-256")


def test_a_backup_is_stale_after_26_hours_and_the_drill_checks_both_copies():
    import ops
    import scheduler
    assert ops.BACKUP_STALE_HOURS == 26
    drill = inspect.getsource(scheduler.run_restore_drill)
    assert "BACKUP_STALE_HOURS" in drill and "_drill_offsite" in drill
    offsite = inspect.getsource(scheduler._drill_offsite)
    assert 'got["sha256"] != row["sha256"]' in offsite and "_decrypt_file_chunked" in offsite
    _says(RECOVERY, "no good backup (or no off-site copy) in 26 hours",
          "fails on a snapshot older than 26 hours")


# ── /health, paging, the monitors ────────────────────────────────────────────

_HEALTH_ERRORS = ("db_unavailable", "db_unreadable", "schema_mismatch", "db_not_writable", "data_missing")
_HEALTH_PROBLEMS = ("db_busy", "db_not_wal", "scheduler_stale", "jobs_overdue", "backup_stale",
                    "offsite_backup_stale")


def test_every_health_code_the_runbook_names_is_one_the_code_answers():
    import status_manager
    src = inspect.getsource(status_manager.health_snapshot)
    for code in _HEALTH_ERRORS + _HEALTH_PROBLEMS:
        assert f'"{code}"' in src, code
    assert 'f"disk_{disk[\'state\']}"' in src
    text = _read(RECOVERY)
    for code in _HEALTH_ERRORS + _HEALTH_PROBLEMS + ("disk_low", "disk_critical"):
        assert code in text, code


def test_health_is_read_only_and_says_ok_or_degraded(db_path, monkeypatch):
    import models
    import ops
    import status_manager as sm
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sm, "DB_PATH", db_path)
    monkeypatch.setattr(ops, "backup_status", None, raising=False)
    sm._schema_ok.clear()
    sent = []
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: sent.append(a) or {"sent": False})
    payload, status = sm.health_snapshot(db_path)
    assert status == 200 and payload["status"] in ("ok", "degraded")
    nested = json.dumps({k: v for k, v in payload.items() if k != "status"})
    assert '"status"' not in nested
    assert sent == []
    assert "check_platform_sla(send=False" in inspect.getsource(sm.health_snapshot)
    _says(RECOVERY, "it is read-only (it writes nothing and sends nothing)", '"status":"ok"')


def test_health_and_the_console_are_never_cached_by_a_browser():
    import http_layer
    assert set(http_layer._NO_STORE_PREFIXES) >= {"/api/", "/mobile/api/", "/admin", "/audit/r/"}
    assert 'path == "/health"' in inspect.getsource(http_layer.add_cache_headers)
    _says(SECURITY, "`cache-control: no-store` on `/api/`, `/mobile/api/`, `/admin`")


def test_paging_texts_will_phone_and_emails_at_most_hourly_on_railway():
    import ops
    src = inspect.getsource(ops.alert_will)
    assert "will_phone()" in src and 'use_case="alert"' in src and "_send_branded" in src
    assert ops.PLATFORM_ALERT_COOLDOWN_MINUTES == 60
    sla = inspect.getsource(ops.check_platform_sla)
    assert "scheduling_allowed()" in sla and "page_operator(" in sla
    _says(RECOVERY, "a text to `will_phone`", "at most once an hour per problem")


def test_the_sla_check_runs_on_the_supervisor_every_five_minutes():
    import platform_monitor
    assert "check_platform_sla(send=True)" in inspect.getsource(platform_monitor.PlatformSupervisor._run_sla)
    assert inspect.signature(platform_monitor.PlatformSupervisor).parameters["sla_every"].default == 300
    _says(RECOVERY, "sent by the web process's supervisor thread every five minutes")


def test_the_dead_man_ping_reports_a_failure_on_slash_fail(monkeypatch):
    import requests
    import ops
    urls = []

    class _Resp:
        status_code = 200

    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: urls.append((url, timeout)) or _Resp())
    assert ops.ping_healthcheck() is True
    assert ops.ping_healthcheck("fail") is True
    assert urls == [("https://hc.example/abc", (3, 5)), ("https://hc.example/abc/fail", (3, 5))]
    monkeypatch.delenv("HEALTHCHECK_PING_URL")
    assert ops.ping_healthcheck() is False
    _says(RECOVERY, "the backup and the digest ping `/fail` when they fail")


def test_the_heartbeat_is_stale_after_fifteen_minutes():
    import jobs_registry
    import status_manager
    assert jobs_registry.HEARTBEAT_STALE_MINUTES == 15
    assert status_manager.SCHEDULER_STALE_MINUTES == 15


# ── the boot guards ──────────────────────────────────────────────────────────

def test_the_boot_refuses_railway_without_its_volume():
    import models
    with pytest.raises(RuntimeError):
        models.require_volume(environ={"RAILWAY_ENVIRONMENT": "production"})
    assert models.require_volume(environ={"RAILWAY_ENVIRONMENT": "production", "ALLOW_NO_VOLUME": "1"}) is None
    assert models.require_volume(environ={}) is None
    _says(RECOVERY, "`allow_no_volume=1` overrides")


def test_an_emptied_volume_is_refused_before_init_db():
    import status_manager
    assert status_manager.VOLUME_MARKER == ".cavnar-volume.json"
    boot = _read("hosted_dashboard.py")
    assert boot.index("assert_platform_not_emptied()") < boot.index("_init_db()")
    _says(RECOVERY, "`.cavnar-volume.json`", "`allow_empty_database=1`")


# ── nothing sends from a backend that is not production ──────────────────────

def test_a_laptop_or_a_pending_restore_sends_nothing(monkeypatch):
    import scheduler
    for v in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
              "ALLOW_LOCAL_SCHEDULER", "RESTORE_FROM"):
        monkeypatch.delenv(v, raising=False)
    assert scheduler.scheduling_allowed() is False
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    assert scheduler.scheduling_allowed() is True
    monkeypatch.setenv("RESTORE_FROM", "/app/data/backups/x.db")
    assert scheduler.scheduling_allowed() is False
    import admin_routes
    assert "scheduling_allowed()" in inspect.getsource(admin_routes._send_blocked)
    assert "_sending_allowed()" in inspect.getsource(admin_routes._live_actions_refused)
    _says(SECURITY, "refused 409 where `scheduler.scheduling_allowed()` is false")


def test_the_two_documented_ungated_sends_are_still_ungated():
    """SECURITY.md names two admin sends that are not gated on a local
    backend. When one is gated, take it out of the doc and out of this test."""
    import admin_routes
    for fn in (admin_routes.test_alert_sms_route, admin_routes.resend_contract):
        src = inspect.getsource(fn)
        assert "_send_blocked" not in src and "_live_actions_refused" not in src \
            and "scheduling_allowed" not in src, fn.__name__
    _says(SECURITY, "two exceptions today", "/admin/alert-contacts/test/<rid>", "/admin/resend-contract/<rid>")


# ── support masking, the AI trace, the breaker ───────────────────────────────

def test_support_reads_are_masked():
    import admin_ops
    out = admin_ops.redact_for_support({"owner_email": "owner@example.com", "ip_address": "10.1.2.3",
                                        "user_agent": "Mozilla", "owner_phone": "5125550123",
                                        "note": "wrote from owner@example.com about cus_ABCDEFGH1234"})
    assert "owner@example.com" not in json.dumps(out)
    assert out["ip_address"] == "10.1.x.x" and out["user_agent"] == "(hidden)"
    assert out["owner_phone"].endswith("0123") and "5125550123" not in out["owner_phone"]
    assert "cus_ABCDEFGH1234" not in out["note"]
    admin_src = _read("admin_routes.py")
    assert 'resp.headers["X-Redacted"] = "support"' in admin_src
    _says(SECURITY, "x-redacted: support")


def test_the_documented_task_result_exposure_is_still_there():
    """SECURITY.md warns that /admin/api/tasks/<job_id> returns stored job
    results unscrubbed. When that is fixed, update the doc and this test."""
    import admin_routes
    src = inspect.getsource(admin_routes.admin_api_task)
    assert "read_async_job" in src and "_scrub" not in src
    _says(SECURITY, "get /admin/api/tasks/<job_id>")


def test_ai_trace_text_is_admin_only_and_kept_thirty_days():
    import admin_routes
    import ai_utils
    assert ai_utils.AI_TRACE_DAYS == 30
    assert "Admins only" in inspect.getsource(admin_routes.admin_api_ai_call)
    assert "Admins only" not in inspect.getsource(admin_routes.admin_api_ai_calls)
    _says(SECURITY, "only a full admin can read the text", "the list, which support can read, carries no text")


def test_the_ai_breaker_reset_is_admin_only_and_this_process_only():
    import admin_routes
    import ai_utils
    src = inspect.getsource(admin_routes.admin_api_ai_reset_breaker)
    assert "Admins only" in src and "reset_breaker(" in src
    assert "_breakers" in inspect.getsource(ai_utils.reset_breaker)
    _says(RECOVERY, "closes it now — in this process only")


# ── outbound ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data",
                                 "http://10.0.0.5/", "http://[::1]/"])
def test_an_outbound_fetch_to_a_private_address_is_refused(url):
    import net_safety
    with pytest.raises(net_safety.UnsafeURL):
        net_safety.vet(url)


def test_the_net_safety_rule_is_the_documented_one():
    import net_safety
    src = inspect.getsource(net_safety)
    assert "allow_redirects" in src
    _says(SECURITY, "redirects are never followed", "the host is resolved once")


# ── the scheduler lease and the deploy ───────────────────────────────────────

def test_the_lease_keeper_timings():
    import ops
    assert ops.LEASE_RENEW_SECONDS == 60
    assert ops.LEASE_OWNER_GONE_SECONDS == 240
    assert ops.SCHEDULER_LEASE_STALE_SECONDS == 1800
    _says(SPLIT, "(**60 s**)", "(**240 s**)", "(**30 minutes**)")


def test_the_deploy_healthcheck_waits_120_seconds():
    deploy = json.loads(_read("railway.json"))["deploy"]
    assert deploy["healthcheckPath"] == "/health" and deploy["healthcheckTimeout"] == 120
    _says(SPLIT, "(**120 seconds**, `railway.json`)", "downtime until a redeploy or a rollback")
    _says(RECOVERY, "within the 120-second healthcheck")


def test_the_worker_boot_the_split_doc_describes():
    src = _read("worker.py")
    assert "init_db()" in src and "ensure_columns()" in src and "_LEASE_KEEPER.start()" in src
    assert "ops.shutdown_scheduler()" in src and "close_orphaned_runs()" in src
    # The doc says the volume guard and the JSON log are NOT yet in worker.py;
    # when they arrive, change this line and the doc's sentence together.
    assert "require_volume" not in src and "logging_setup" not in src
    _says(SPLIT, "as of 9/29/26 it does not yet")
