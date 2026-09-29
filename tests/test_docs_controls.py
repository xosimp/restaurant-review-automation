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
    # Integration wave (INT-2): B2's, H's and B1's sensitive routes, and freeze.
    "/admin/send-reset-link/<int:user_id>",
    "/admin/reset-password/<int:user_id>",
    "/admin/reset-password-by-restaurant/<int:restaurant_id>",
    "/admin/resend-welcome/<int:restaurant_id>",
    "/admin/freeze/<int:restaurant_id>",
    "/admin/api/client/<int:restaurant_id>/demo",
    "/admin/api/client/<int:restaurant_id>/delete-demo",
    "/admin/api/client/<int:restaurant_id>/delete",
    "/admin/api/brand/add-location",
    "/admin/api/billing/<int:restaurant_id>/change-plan",
    "/admin/api/billing/<int:restaurant_id>/mark-signed",
    "/admin/api/billing/<int:restaurant_id>/attach-stripe-customer",
    "/admin/api/billing/<int:restaurant_id>/lift-hold",
    "/admin/toast/save/<int:restaurant_id>",
    "/admin/toast/disconnect/<int:restaurant_id>",
    "/admin/square/save/<int:restaurant_id>",
    "/admin/square/disconnect/<int:restaurant_id>",
    "/admin/clover/save/<int:restaurant_id>",
    "/admin/clover/disconnect/<int:restaurant_id>",
    "/admin/rpower/save/<int:restaurant_id>",
    "/admin/rpower/bootstrap/<int:restaurant_id>",
    "/admin/rpower/disconnect/<int:restaurant_id>",
}

# Routes that need the step-up for only PART of what they do, through
# auth.reauth_refusal rather than the decorator (INT-2): the legacy settings
# save (a billing, module or owner-email change), the offboarding steps that
# act (integrations, Stripe, DocuSign), the review-account seed's rotation.
_STEP_UP_IN_PART = ("save_client_settings", "admin_api_offboarding_step", "seed_review_account_route")


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
    import admin_routes
    import offboarding
    for name in _STEP_UP_IN_PART:
        assert "reauth_refusal(" in inspect.getsource(getattr(admin_routes, name)), name
    assert tuple(admin_routes._SETTINGS_STEP_UP_FIELDS) == (
        "billing_status", "module_reviews", "module_labor", "module_inventory", "module_marketing", "owner_email")
    assert tuple(offboarding.ACTING_STEPS) == ("integrations", "stripe", "docusign")
    _says(SECURITY, "applied today", f"the decorator, on {len(_STEP_UP_TODAY)} routes",
          "`offboarding.acting_steps`: integrations, stripe, docusign",
          "the billing status, a module switch or the owner email (`_settings_step_up_fields`)")


def test_an_expired_console_read_is_401_json_and_a_page_is_a_redirect():
    """A fetch() under /admin/api/ that met an expired session got a 302 to
    the HTML login page, which fetch followed and failed to parse (INT-2):
    it answers 401 {session_expired} like a write; a page still redirects."""
    from flask import Blueprint, Flask
    import auth
    app = Flask(__name__)
    login_bp = Blueprint("auth", __name__)
    login_bp.add_url_rule("/login", "login", lambda: "login")
    app.register_blueprint(login_bp)

    @auth.admin_required
    def view(current_user=None):
        return "reached"

    with app.test_request_context("/admin/api/clients/list", method="GET"):
        resp, status = view()
        assert status == 401 and resp.get_json()["session_expired"] is True
    with app.test_request_context("/admin/api/anything", method="POST"):
        assert view()[1] == 401
    with app.test_request_context("/admin", method="GET"):
        resp = view()
        assert resp.status_code == 302 and resp.location.endswith("/login")
    _says(SECURITY, "**401 `{session_expired: true}`**", "a page get is a **302** to the login page")


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
    """One welcome email, whichever sender (INT-2): the outbox after signing
    or a checkout, and the console's Resend welcome, all call
    emails.send_welcome_with_set_password_link, which mints the link at send
    time for models.SET_PASSWORD_LINK_HOURS (the resend used to mint a
    one-hour link of its own)."""
    import admin_routes
    import billing_jobs
    import emails
    import models
    assert models.SET_PASSWORD_LINK_HOURS == 72
    assert billing_jobs.WELCOME_LINK_HOURS == models.SET_PASSWORD_LINK_HOURS
    assert emails.SET_PASSWORD_LINK_DAYS * 24 == models.SET_PASSWORD_LINK_HOURS
    assert inspect.signature(models.create_set_password_token).parameters["hours"].default == 72
    assert "create_set_password_token(user[\"id\"], db_path=dbp)" in inspect.getsource(
        emails.send_welcome_with_set_password_link)
    assert "send_welcome_with_set_password_link(" in inspect.getsource(admin_routes.resend_welcome_email)
    assert "send_welcome_with_set_password_link(" in inspect.getsource(billing_jobs)
    _says(SECURITY, "temporary passwords are never stored or emailed",
          "one-use set-password link valid for 72 hours from the send (`models.set_password_link_hours`")


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


def test_a_break_glass_unlock_says_when_no_login_has_the_name(db_path, capsys):
    """A misspelt LOGIN_UNLOCK_USERNAMES used to print "unlock" while the
    real login stayed locked; it says the name is no login (INT-2)."""
    import auth
    import security
    auth.init_auth(db_path)
    assert security.apply_boot_unlocks(env={"LOGIN_UNLOCK_USERNAMES": "no-such-login"}, db_path=db_path) == []
    assert "NO login is named 'no-such-login'" in capsys.readouterr().out
    _says(SECURITY, "prints `no login is named '<name>'` in the boot log", "records `login_exists`")


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
                                                "app_secrets", "view_as_sessions", "user_backup_codes",
                                                "async_jobs"}
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
_HEALTH_PROBLEMS = ("db_busy", "db_not_wal", "scheduler_stale", "scheduler_wedged", "scheduler_stalled",
                    "jobs_overdue", "backup_stale", "offsite_backup_stale")


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


def test_every_admin_send_is_gated_and_the_doc_names_the_deliberate_exceptions():
    """The alert-contact test and the contract resend were the two admin
    sends a local backend let through; INT-2 gated both with _send_blocked,
    mark-signed now checks before it writes, and three kinds of send are
    left ungated on purpose — each named in SECURITY.md."""
    import admin_routes
    import client_api
    for fn in (admin_routes.test_alert_sms_route, admin_routes.resend_contract, admin_routes.resend_welcome_email,
               admin_routes.test_digest, admin_routes.test_urgent, admin_routes._send_reset_link_to):
        assert "_send_blocked()" in inspect.getsource(fn), fn.__name__
    signed = inspect.getsource(admin_routes.admin_api_billing_mark_signed)
    assert signed.index("_live_actions_refused()") < signed.index("update_restaurant(")
    assert "_send_blocked" not in inspect.getsource(admin_routes.admin_two_factor_send)
    assert '"deletion.notice_skipped"' in _read("client_api.py") and client_api
    _says(SECURITY, "refused, 409, one shared sentence", "checked before anything is written",
          "deliberately not gated", "`post /admin/two-factor/send`", "`post /api/send-referral`",
          "recorded as `deletion.notice_skipped`")


def test_a_copy_of_the_app_never_loads_another_checkouts_env():
    """Flask's own .env lookup walks up from the working directory, so a
    worktree's copy loaded the main checkout's .env (production's keys)."""
    src = _read("hosted_dashboard.py")
    assert 'load_dotenv(pathlib.Path(__file__).parent / ".env")' in src
    assert "app.run(host=\"0.0.0.0\", port=PORT, debug=False, load_dotenv=False)" in src
    _says(SECURITY, "`app.run(..., load_dotenv=false)`")


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
    import sales_audit_routes
    assert "_admin_support_redaction(resp)" in inspect.getsource(sales_audit_routes._support_redaction)
    _says(SECURITY, "x-redacted: support", "`sales_audit_routes._support_redaction`")


def test_support_is_refused_the_legacy_client_pages():
    import admin_routes
    page, status = admin_routes._legacy_page_refused({"id": 9, "is_admin": 0, "role": "support"})
    assert status == 403 and "Use the admin console" in page
    assert admin_routes._legacy_page_refused({"id": 1, "is_admin": 1}) is None
    for fn in (admin_routes.client_settings_page, admin_routes.client_data_page):
        assert "_legacy_page_refused(current_user)" in inspect.getsource(fn), fn.__name__
    _says(SECURITY, "support gets a 403 page that points at the console instead (`admin_routes._legacy_page_refused`)")


def test_the_audit_keeps_only_a_phones_last_four():
    import admin_events
    out = admin_events._redact({"phone": "(512) 555-0123", "to_phone": "+15125550199", "phone_last4": "0123",
                                "password": "hunter2hunter2"})
    assert out["phone"] == "…0123" and out["to_phone"] == "…0199" and out["phone_last4"] == "0123"
    assert out["password"] == "[redacted]"
    _says(SECURITY, "keeps only the last four digits of a value under any key naming a phone (`admin_events._redact`)")


def test_the_owner_pos_connects_refuse_a_store_bound_elsewhere():
    import clover_routes
    import mobile_api
    import square_routes
    import toast_routes
    for fn in (toast_routes.client_save_toast, square_routes.client_save_square, clover_routes.client_save_clover,
               mobile_api.mobile_connect_toast, mobile_api.mobile_connect_square, mobile_api.mobile_connect_clover):
        assert "owner_pos_binding_refusal(" in inspect.getsource(fn), fn.__name__
    _says(SECURITY, "the owner's own connects refuse it too, 409 (`models.owner_pos_binding_refusal`")


def test_login_history_is_on_the_retention_registry():
    import ops
    assert ops._RETENTION_DAYS["login_history"] == 90
    assert ops._RETENTION_COLUMN.get("login_history", "created_at") == "created_at"
    _says(SECURITY, "`login_history` (90 days) among them")


def test_the_task_poll_is_admin_only_and_serves_only_task_results():
    """/admin/api/tasks/<job_id> returned any stored async job, unscrubbed,
    to support logins too; it is admin-only now, serves only ops' admin
    tasks (the fetch-now and POS sync), and only the keys a poll reads
    (INT-2). /admin/api/admin-jobs/<id> is the one path for a once-only value."""
    import admin_routes
    src = inspect.getsource(admin_routes.admin_api_task)
    assert 'current_user.get("is_admin")' in src and "_ADMIN_TASK_KINDS" in src and "_ADMIN_TASK_RESULT_KEYS" in src
    assert "password_once" not in admin_routes._ADMIN_TASK_RESULT_KEYS
    assert tuple(admin_routes._ADMIN_TASK_KINDS) == ("review_fetch_one", "pos_sync_one")
    _says(SECURITY, "get /admin/api/tasks/<job_id>` is 403 for support",
          "(`_admin_task_kinds`: `review_fetch_one`, `pos_sync_one`")


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


# ── the scheduler's supervisor, billing jobs, offboarding ─────────────────────

def test_the_supervisor_restarts_the_scheduler_with_its_lease_keeper():
    """A restarted loop without the lease keeper let its lease look
    abandoned after 4 minutes (D #134): the supervisor starts the thread
    body start_scheduler uses, with backoff, and flips /status's scheduler
    row while the heartbeat is stale."""
    import platform_monitor
    import scheduler
    src = _read("hosted_dashboard.py")
    body = src[src.index("def _restart_scheduler_loop"):]
    body = body[:body.index("\ndef ", 1)]
    assert "target=_sched_mod._run_scheduler_thread" in body
    assert "_LEASE_KEEPER.start()" in inspect.getsource(scheduler._run_scheduler_thread)
    assert platform_monitor.PlatformSupervisor()._backoff == 30.0
    assert "min(600.0, self._backoff * 2)" in inspect.getsource(platform_monitor.PlatformSupervisor._watch_scheduler)
    assert "check_scheduler_liveness" in inspect.getsource(platform_monitor.PlatformSupervisor._check_liveness)
    _says(RECOVERY, "the lease keeper and then the loop (`scheduler._run_scheduler_thread`)",
          "backoff (30 seconds, doubling to 10 minutes)", "(`status_manager.check_scheduler_liveness`)")


def test_the_billing_jobs_the_runbook_names_are_registered():
    import jobs_registry
    assert jobs_registry.JOBS["stripe_reconcile"]["target"] == ("billing_jobs", "reconcile_stripe")
    assert jobs_registry.JOBS["owed_sends"]["cadence"] == "every tick"
    assert jobs_registry.JOBS["provider_probes"]["target"] == ("provider_health", "run_probes")
    assert "reconcile_stripe" not in jobs_registry.JOBS
    _says(RECOVERY, "the `stripe_reconcile` job (3:30am ct, `billing_jobs.reconcile_stripe`)",
          "the `owed_sends` job drains due rows on every tick", "probed hourly by the `provider_probes` job")


def test_the_offboarding_steps_that_act_and_their_refusals():
    import offboarding
    src = inspect.getsource(offboarding.set_step)
    assert 'code = 409 if (detail.get("covered") or detail.get("local_backend")) else 502' in src
    void = inspect.getsource(offboarding._void_open_envelope)
    assert '"local_backend": True' in void and "already signed" in void and "}, 502)" in void
    delete = inspect.getsource(offboarding.delete_restaurant_now)
    assert "admin_homes(" in delete and 'state["outstanding"]' in delete
    withdraw = inspect.getsource(offboarding.withdraw_deletion_request)
    assert withdraw.count("}, 409") == 2
    _says(RECOVERY, "409 for a location billed under another's subscription",
          "when the envelope is already signed (skip it with that note)",
          "409 if there is none, or it changed while you looked",
          "while an admin or support login calls it home")


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
    # The volume guard and the JSON log open main(), before the schema is
    # touched (the integration wave, 9/29/26), as the doc says.
    assert src.index("logging_setup.configure()") < src.index("models.require_volume()") < src.index("init_db()")
    assert "basicConfig" not in src
    _says(SPLIT, "worker.py calls both at the top of main()")
