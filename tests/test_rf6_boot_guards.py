"""Blind re-audit 10/27/26 (RF6) — the boot's checks of its own deployment.

- /health's schema check: a missing column on a non-strict table degrades
  with a named gap; the sign-in tables, a missing table, stay a 500.
- A production-looking Railway service whose environment name stands its
  scheduler and sends down says so: CRITICAL at boot, not_production_env on
  /health. A correctly named one changes nothing.
- The deploy's drain against the shutdown: an ERROR at boot, `shutdown` on
  /health, never a problem.
- A DATABASE_URL set but unusable on Railway raises instead of quietly
  serving the SQLite file (the frozen pre-cutover copy there).
- The emptied-database guard reads the database the app serves, refuses a
  boot that would serve SQLite after Postgres held the clients, and fails
  closed when its marker cannot be read from object storage.
- The restore guard fails closed on an unreadable object store."""
import os
import sqlite3

import pytest

import config
import marker_store
import models
import status_manager

PROD = {"RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_PROJECT_ID": "p"}


# ── /health: strict and soft schema gaps ─────────────────────────────────────

def test_split_schema_gaps():
    strict, soft = status_manager.split_schema_gaps(
        ["users.email_change_pending", "reviews.summary", "brand_new_table", "sessions.client_app",
         "guest_contacts.tock_id"])
    assert strict == ["users.email_change_pending", "brand_new_table", "sessions.client_app"]
    assert soft == ["reviews.summary", "guest_contacts.tock_id"]


@pytest.fixture
def reference(monkeypatch):
    """A small reference schema, so the test owns exactly what is expected."""
    ref = {"users": {"id", "email_change_pending"}, "service_status": {"id", "name", "rf6_probe"},
           "rf6_needed_table": {"id"}}
    monkeypatch.setitem(status_manager._reference_schema, "cols", ref)
    status_manager._schema_ok.clear()
    yield ref
    status_manager._schema_ok.clear()


def test_a_missing_column_on_a_soft_table_degrades_and_is_named_in_the_log(db_path, reference, caplog):
    reference.pop("rf6_needed_table")
    with caplog.at_level("ERROR", logger="status_manager"):
        payload, status = status_manager.health_snapshot(db_path)
    assert "service_status.rf6_probe" in caplog.text
    assert status == 200 and "schema_gaps" in payload["problems"], payload
    assert payload["schema_gaps_count"] == 1 and payload["status"] == "degraded"
    assert "rf6_probe" not in str(payload), "names go to the log, not the public body (#134)"


def test_a_missing_table_is_still_a_500(db_path, reference):
    reference["service_status"].discard("rf6_probe")
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 500 and payload["error"] == "schema_mismatch"


def test_a_missing_sign_in_column_is_still_a_500(db_path, reference):
    reference.pop("rf6_needed_table")
    reference["service_status"].discard("rf6_probe")
    reference["users"].add("rf6_login_probe")
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 500 and payload["error"] == "schema_mismatch" and payload["missing_count"] == 1


def test_a_clean_reference_is_ok_and_carries_no_gap_keys(db_path, reference):
    reference.pop("rf6_needed_table")
    reference["service_status"].discard("rf6_probe")
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and "schema_gaps_count" not in payload and "schema_gaps" not in payload["problems"]


# ── the environment name ─────────────────────────────────────────────────────

LIVE_STRIPE = {"STRIPE_SECRET_KEY": "sk_live_x"}
PROD_DB = {"DATABASE_URL": "postgresql://u:p@postgres.railway.internal:5432/railway"}


@pytest.mark.parametrize("env, flagged", [
    (dict(PROD, **LIVE_STRIPE), False),                                        # production: fine
    ({"RAILWAY_ENVIRONMENT_NAME": "prod", "RAILWAY_PROJECT_ID": "p", **LIVE_STRIPE}, True),
    ({"RAILWAY_ENVIRONMENT_NAME": "prod", "RAILWAY_PROJECT_ID": "p", **PROD_DB}, True),
    ({"RAILWAY_ENVIRONMENT_NAME": "prod", "RAILWAY_PROJECT_ID": "p", "CAVNAR_PRODUCTION_SERVICE": "1"}, True),
    ({"RAILWAY_ENVIRONMENT_NAME": "prod", "RAILWAY_PROJECT_ID": "p"}, False),   # nothing says production
    ({"RAILWAY_ENVIRONMENT_NAME": "staging", "RAILWAY_PROJECT_ID": "p", **LIVE_STRIPE, **PROD_DB}, False),
    ({"RAILWAY_ENVIRONMENT_NAME": "cavnar-pr-12", "RAILWAY_PROJECT_ID": "p", **PROD_DB}, False),
    (dict(PROD, CAVNAR_ENV="prod", **LIVE_STRIPE), True),                       # a CAVNAR_ENV typo
    (dict(PROD, CAVNAR_ENV="staging", **LIVE_STRIPE), False),                   # deliberate (the restore drill)
    (dict(PROD, CAVNAR_ENV="production"), False),
    ({"STRIPE_SECRET_KEY": "sk_live_x"}, False),                                # off Railway
])
def test_environment_name_problem(env, flagged):
    problem = config.environment_name_problem(env)
    assert bool(problem) is flagged, problem
    if flagged:
        assert "CAVNAR_ENV" in problem and "stand down" in problem


def test_health_says_not_production_env_and_the_boot_says_it_loudly(db_path, monkeypatch, caplog):
    for k in list(os.environ):
        if k.startswith("RAILWAY_") and k != "RAILWAY_VOLUME_MOUNT_PATH":
            monkeypatch.delenv(k)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "prod")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    monkeypatch.setenv("CAVNAR_PRODUCTION_SERVICE", "1")
    monkeypatch.delenv("CAVNAR_ENV", raising=False)
    status_manager._schema_ok.clear()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and "not_production_env" in payload["problems"]
    assert "env_problem" not in payload, "the sentence stays out of a public endpoint"
    captured = []
    import ops
    monkeypatch.setattr(ops, "capture", lambda exc, **k: captured.append((str(exc), k.get("job"))))
    with caplog.at_level("INFO", logger="status_manager"):
        out = status_manager.boot_environment_report(db_path)
    assert out["deploy_env"] == "prod" and out["scheduling_allowed"] is False
    assert out["env_problem"] and captured and captured[0][1] == "boot_environment"
    assert any(r.levelname == "CRITICAL" and "deployment environment" in r.getMessage() for r in caplog.records)
    assert any("deployment: env=prod" in r.getMessage() for r in caplog.records)


def test_a_correctly_named_production_reports_nothing(db_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("RAILWAY_") and k != "RAILWAY_VOLUME_MOUNT_PATH":
            monkeypatch.delenv(k)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_DRAINING_SECONDS", "60")
    monkeypatch.delenv("CAVNAR_ENV", raising=False)
    status_manager._schema_ok.clear()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and "not_production_env" not in payload["problems"]
    assert payload["shutdown"] == {"drain_seconds": 60, "needs_seconds": config.shutdown_seconds_needed(),
                                   "fits": True}
    out = status_manager.boot_environment_report(db_path)
    assert out["env_problem"] is None and out["drain_problem"] is None


def test_off_railway_the_report_is_silent_and_health_has_no_shutdown_key(db_path):
    assert not config.railway_marked()
    assert status_manager.boot_environment_report(db_path) == {"railway": False, "env_problem": None,
                                                               "drain_problem": None}
    payload, _ = status_manager.health_snapshot(db_path)
    assert "shutdown" not in payload


# ── the drain ────────────────────────────────────────────────────────────────

def test_drain_budget_problem():
    assert config.drain_budget_problem({}) is None                              # off Railway
    unset = config.drain_budget_problem(dict(PROD))
    assert unset and "unset" in unset and "default is 0" in unset
    short = config.drain_budget_problem(dict(PROD, RAILWAY_DEPLOYMENT_DRAINING_SECONDS="20"))
    assert short and "is 20 s" in short
    assert config.drain_budget_problem(dict(PROD, RAILWAY_DEPLOYMENT_DRAINING_SECONDS="60")) is None
    assert config.drain_budget_problem(dict(PROD, RAILWAY_DEPLOYMENT_DRAINING_SECONDS="40",
                                            CAVNAR_SHUTDOWN_SECONDS="45"))


def test_an_unset_drain_logs_an_error_at_boot_but_is_no_health_problem(db_path, monkeypatch, caplog):
    for k in list(os.environ):
        if k.startswith("RAILWAY_") and k != "RAILWAY_VOLUME_MOUNT_PATH":
            monkeypatch.delenv(k)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    with caplog.at_level("ERROR", logger="status_manager"):
        out = status_manager.boot_environment_report(db_path)
    assert out["drain_problem"] and any("shutdown budget" in r.getMessage() for r in caplog.records)
    status_manager._schema_ok.clear()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and payload["shutdown"]["fits"] is False and payload["shutdown"]["drain_seconds"] is None
    assert not any("drain" in p or "shutdown" in p for p in payload["problems"])


# ── a DATABASE_URL that cannot be used ───────────────────────────────────────

@pytest.fixture
def no_suite_routes(monkeypatch):
    monkeypatch.setattr(models, "_PG_ROUTES", {})
    for v in config.RAILWAY_MARKERS + config.RAILWAY_CONTAINER_HINTS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_PG", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)


@pytest.mark.parametrize("env, fatal", [
    ({"DATABASE_URL": "${{Postgres.DATABASE_URL}}", "RAILWAY_ENVIRONMENT_NAME": "production"}, True),
    ({"DATABASE_URL": "mysql://x/y", "RAILWAY_PROJECT_ID": "p"}, True),
    ({"DATABASE_URL": "postgresql://u@h/db", "RAILWAY_DEPLOYMENT_ID": "d"}, True),     # markers lost
    ({"DATABASE_URL": "postgresql://u@h/db", "RAILWAY_DEPLOYMENT_ID": "d", "ALLOW_LOCAL_PG": "1"}, False),
    ({"DATABASE_URL": "postgresql://u@h/db", "RAILWAY_PROJECT_ID": "p"}, False),       # production: fine
    ({"DATABASE_URL": "mysql://x/y", "RAILWAY_PROJECT_ID": "p", "DB_BACKEND": "sqlite"}, False),
    ({"DATABASE_URL": "postgresql://prod/db"}, False),                                 # local: A01-06 as before
    ({"DATABASE_URL": "postgresql://prod/db", "RAILWAY_VOLUME_MOUNT_PATH": "/x"}, False),   # the suite's own
    ({"RAILWAY_PROJECT_ID": "p"}, False),
])
def test_database_url_problem(env, fatal):
    assert bool(config.database_url_problem(env)) is fatal


def test_an_unresolved_database_url_on_railway_raises_instead_of_serving_sqlite(no_suite_routes, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("DATABASE_URL", "${{Postgres.DATABASE_URL}}")
    with pytest.raises(RuntimeError, match="not a Postgres URL"):
        models.get_conn()
    monkeypatch.setenv("DB_BACKEND", "sqlite")                   # the deliberate rollback to SQLite
    c = models.get_conn()
    c.close()


def test_a_marker_less_process_on_the_container_raises(no_suite_routes, monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@postgres.railway.internal:5432/railway")
    with pytest.raises(RuntimeError, match="carries none of Railway's markers"):
        models._db_backend()


def test_off_railway_a_refused_url_still_falls_back_quietly(no_suite_routes, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod-user:pw@prod.example.com/railway")
    assert models._db_backend() == "sqlite"


# ── the emptied-database guard ───────────────────────────────────────────────

@pytest.fixture
def volume(tmp_path, monkeypatch):
    """The platform's database on a volume of its own (DB_PATH), a client in it."""
    import shutil
    from conftest import _db_template
    vol = tmp_path / "vol"
    vol.mkdir()
    path = str(vol / "reviews.db")
    shutil.copyfile(_db_template(), path)
    monkeypatch.setattr(models, "DB_PATH", path)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(vol))
    monkeypatch.delenv("ALLOW_EMPTY_DATABASE", raising=False)
    routes = dict(models._PG_ROUTES)
    monkeypatch.setattr(models, "_PG_ROUTES", {})
    yield path
    models._PG_ROUTES.update(routes)


@pytest.mark.sqlite_only     # the volume's own SQLite file
def test_the_marker_names_its_backend_and_sqlite_after_postgres_is_refused(volume, monkeypatch):
    models.create_restaurant(models.Restaurant(name="Client", owner_email="c@x.test", billing_status="active"),
                             db_path=volume)
    marker = status_manager.record_volume_marker()
    assert marker and marker["backend"] == "sqlite"
    status_manager.assert_platform_not_emptied()                 # as before: clients here, SQLite
    import json
    with open(status_manager.volume_marker_path(), encoding="utf-8") as f:
        body = json.load(f)
    body["backend"] = "postgres"                                 # Postgres held them...
    with open(status_manager.volume_marker_path(), "w", encoding="utf-8") as f:
        json.dump(body, f)
    with pytest.raises(RuntimeError, match="Refusing to boot on SQLite"):
        status_manager.assert_platform_not_emptied()             # ...and DATABASE_URL is gone
    monkeypatch.setenv("DB_BACKEND", "sqlite")                   # the deliberate rollback
    status_manager.assert_platform_not_emptied()


@pytest.mark.sqlite_only
def test_an_emptied_database_is_still_refused(volume):
    rid = models.create_restaurant(models.Restaurant(name="Client", owner_email="c@x.test",
                                                     billing_status="active"), db_path=volume)
    status_manager.record_volume_marker()
    conn = sqlite3.connect(volume)
    conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="no client restaurants"):
        status_manager.assert_platform_not_emptied()


# ── failing closed on object storage ─────────────────────────────────────────

@pytest.fixture
def broken_bucket(monkeypatch):
    """A volume-less Railway service whose object store answers 500."""
    import offsite_backup
    for k, v in {"RAILWAY_ENVIRONMENT": "production", "VOLUME_OPTIONAL": "1",
                 "BACKUP_S3_ENDPOINT": "https://r2.example.com", "BACKUP_S3_BUCKET": "b",
                 "BACKUP_S3_ACCESS_KEY_ID": "k", "BACKUP_S3_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.delenv("RESTORE_GUARD", raising=False)
    state = {"down": True, "store": {}}

    def get(key, cfg):
        if state["down"]:
            raise offsite_backup.OffsiteError("object store refused the blob: HTTP 500")
        if cfg["prefix"] + key not in state["store"]:
            raise offsite_backup.OffsiteError("object store refused the blob: HTTP 404")
        return state["store"][cfg["prefix"] + key]

    def put(key, data, cfg, content_type=None):
        if state["down"]:
            raise offsite_backup.OffsiteError("HTTP 500")
        state["store"][cfg["prefix"] + key] = bytes(data)
    monkeypatch.setattr(offsite_backup, "get_bytes", get)
    monkeypatch.setattr(offsite_backup, "put_bytes", put)
    monkeypatch.setattr(offsite_backup, "delete_object", lambda key, cfg: state["store"].pop(cfg["prefix"] + key, None))
    marker_store._exists_cache.clear()
    yield state
    marker_store._exists_cache.clear()


def test_strict_reads_tell_unreadable_from_absent(broken_bucket):
    assert marker_store.read("m.json") is None                   # non-strict: as before
    with pytest.raises(marker_store.MarkerUnreadable):
        marker_store.read("m.json", strict=True)
    broken_bucket["down"] = False
    assert marker_store.read("m.json", strict=True) is None      # a 404 is "absent", strict or not


def test_an_unreadable_freeze_holds_scheduling_and_sends(broken_bucket, monkeypatch):
    monkeypatch.delenv("RESTORE_FROM", raising=False)
    monkeypatch.delenv("MAINTENANCE_READ_ONLY", raising=False)
    assert config.restore_frozen() is True and config.scheduling_allowed() is False
    broken_bucket["down"] = False
    marker_store._exists_cache.clear()
    assert config.restore_frozen() is False and config.scheduling_allowed() is True


def test_the_restore_guard_holds_on_an_unreadable_store_and_never_rebaselines(broken_bucket, db_path, monkeypatch):
    import restore_guard
    monkeypatch.setattr(models, "DB_PATH", db_path)
    assert restore_guard.active() is True
    out = restore_guard.check_and_stamp(db_path)
    assert out["state"] == "frozen" and out.get("unreadable") is True
    # The freeze readable but the epoch not: an error, and nothing stamped.
    broken_bucket["down"] = False
    real = marker_store.read

    def epoch_down(name, strict=False):
        if name == restore_guard.EPOCH_FILE:
            raise marker_store.MarkerUnreadable("epoch: HTTP 500")
        return real(name, strict=strict)
    monkeypatch.setattr(marker_store, "read", epoch_down)
    out = restore_guard.check_and_stamp(db_path)
    assert out["state"] == "error"
    conn = sqlite3.connect(db_path) if not models._PG_ROUTES.get(db_path) else None
    if conn is not None:
        try:
            assert conn.execute("SELECT 1 FROM job_cursors WHERE key='restore_epoch'").fetchone() is None
        finally:
            conn.close()


@pytest.mark.sqlite_only
def test_an_unreadable_volume_marker_refuses_a_boot_with_no_clients(broken_bucket, tmp_path, monkeypatch):
    import shutil
    from conftest import _db_template
    path = str(tmp_path / "reviews.db")
    shutil.copyfile(_db_template(), path)
    monkeypatch.setattr(models, "DB_PATH", path)
    monkeypatch.setattr(models, "_PG_ROUTES", {})
    monkeypatch.delenv("ALLOW_EMPTY_DATABASE", raising=False)
    with pytest.raises(RuntimeError, match="could not be read from object storage"):
        status_manager.assert_platform_not_emptied()
    models.create_restaurant(models.Restaurant(name="Client", owner_email="c@x.test", billing_status="active"),
                             db_path=path)
    status_manager.assert_platform_not_emptied()                 # clients here: boots, with a warning
