"""Integration wave INT-1 — ops and platform wiring between the workstreams:
a job run's log and AI context (F #38, G #148), the traceback a capture
logs, the platform check paging on broken messaging (E), the morning
brief's retry (E #82), the AI ledger's view-as attribution (A), and the
worker's boot (F #108). The pre-shift text's restaurant (E #14) is tested
beside the nudge's own tests, in tests/test_workflow_queue.py."""
import logging
import sqlite3

import pytest

import ai_utils
import models
import ops


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    return db_path


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


# ── a job run carries its name into the log and its attribution into the AI ledger

def test_a_job_run_is_attributed_to_the_scheduler_with_its_own_correlation_id(db):
    seen = {}

    def job():
        import logging_setup
        seen["ai"] = ai_utils.current_ai_context()
        seen["log"] = logging_setup.current()
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    ops.run_job("attr_job", job)
    run_id = _q(db, "SELECT id FROM job_runs WHERE job='attr_job'")[0]["id"]
    assert seen["ai"]["trigger"] == "scheduler" and seen["ai"]["correlation_id"] == f"job:attr_job:{run_id}"
    assert seen["log"]["job"] == "attr_job"
    assert ai_utils.current_ai_context() == {}, "the context leaked out of the run"


def test_an_admins_run_keeps_the_admins_attribution(db):
    seen = {}

    def job():
        seen.update(ai_utils.current_ai_context())
        return True
    with ai_utils.ai_context(trigger="admin", actor_user_id=7, correlation_id="run_now:x:1"):
        ops.run_job("admin_job", job)
    assert (seen["trigger"], seen["actor_user_id"], seen["correlation_id"]) == ("admin", 7, "run_now:x:1")


def test_a_captured_exception_logs_its_traceback_redacted(db, caplog):
    def boom():
        raise RuntimeError("403 for url: https://maps.googleapis.com/x?key=AIzaSySECRET123456789012345")
    with caplog.at_level(logging.WARNING, logger="ops"):
        try:
            boom()
        except RuntimeError as e:
            ops.capture(e, job="places_thing", context="restaurant_id=4")
    rec = next(r for r in caplog.records if r.getMessage().startswith("captured places_thing failure"))
    assert "Traceback" in rec.traceback and "boom" in rec.traceback and rec.restaurant_id == 4
    assert "AIzaSySECRET" not in rec.traceback and "AIzaSySECRET" not in rec.getMessage()


# ── the platform check pages on broken messaging (E) ───────────────────────

def test_the_platform_check_pages_on_broken_messaging_but_health_does_not_read_it(db, monkeypatch):
    import notify
    import scheduler
    import status_manager
    monkeypatch.setattr(status_manager, "DB_PATH", db)
    status_manager.record_scheduler_heartbeat(loop_completed=True)
    monkeypatch.setattr(status_manager, "disk_state", lambda p=None: {"state": "ok"})
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {"state": "ok"})
    asked = []
    monkeypatch.setattr(notify, "messaging_problems",
                        lambda db_path=None: asked.append(1) or ["Twilio delivery reports webhook: 3 refused"])
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: pages.append(lines) or {"sent": True})
    quiet = ops.check_platform_sla(send=False)
    assert asked == [] and quiet["messaging"] == [] and not quiet["alerted"]
    out = ops.check_platform_sla(send=True)
    assert out["alerted"] and any(line.startswith("Messaging: Twilio") for line in pages[0])


# ── the morning brief gives its day back on a transient failure (E #82) ────

def test_a_brief_that_failed_transiently_is_retried_a_few_times(db, monkeypatch):
    import morning_brief
    rid = models.create_restaurant(models.Restaurant(name="Brief Co", owner_email="b@x.test"), db_path=db)
    monkeypatch.setattr(morning_brief, "_brief_queue", lambda db_path=None: ([(rid, "2026-09-29")], []))
    monkeypatch.setattr(morning_brief, "deliver", lambda *a, **k: {"sent": 0, "failed": 1, "retry": True})
    outs = [morning_brief.run_due(db_path=db) for _ in range(morning_brief.BRIEF_RETRIES_PER_DAY + 1)]
    assert [o["failed"] for o in outs[:-1]] == [1] * morning_brief.BRIEF_RETRIES_PER_DAY
    assert outs[-1]["ok"] == 1 and outs[-1]["failed"] == 0, "retried past its daily allowance"
    assert ops.period_claimed(f"morning_brief:{rid}", "2026-09-29")


# ── the AI ledger's actor for a view-as request (A) ────────────────────────

def test_a_view_as_request_is_the_acting_admins(db, monkeypatch):
    import auth
    from flask import Flask
    auth.init_auth(db_path=db)
    hq = models.create_restaurant(models.Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db)
    admin = auth.create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db)
    rid = models.create_restaurant(models.Restaurant(name="Client Grill", owner_email="o@x.test"), db_path=db)
    owner = auth.create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db)
    token = auth.create_view_as_session(owner, {"id": admin}, read_only=False, db_path=db)
    app = Flask(__name__)
    with app.test_request_context("/api/reviews", headers={"Cookie": f"session_token={token}"}):
        trigger, actor, got_rid = ai_utils._request_attribution()
    assert (trigger, actor, got_rid) == ("admin", admin, rid)
    # The decorator's context on flask.g wins, with no read (INT-2 #4).
    from flask import g
    with app.test_request_context("/api/reviews"):
        g.view_as = {"acting_admin_id": 77, "restaurant_id": rid}
        assert ai_utils._request_attribution() == ("admin", 77, rid)


# ── the worker boots like the web process (F #108, #38) ────────────────────

def test_the_worker_checks_the_volume_and_configures_logging_before_anything():
    import inspect
    import worker
    src = inspect.getsource(worker.main)
    assert src.index("logging_setup.configure()") < src.index("models.require_volume()") < src.index("init_db()")
    assert "basicConfig" not in inspect.getsource(worker)


# ── the system card's size trend reads the nightly record (F #28 meets D) ──

def test_the_system_cards_size_trend_reads_the_nightly_record(db):
    import platform_monitor
    assert platform_monitor._size_trend(db)["source"] == "boots", "no nightly record yet"
    mb = 1024 * 1024
    c = sqlite3.connect(db)
    c.execute("INSERT INTO backup_runs (started_at, db_bytes, wal_bytes, backups_bytes, free_bytes) "
              "VALUES (datetime('now','-2 days'), ?, 0, 0, ?)", (100 * mb, 10 * 1024 * mb))
    c.execute("INSERT INTO backup_runs (started_at, db_bytes, wal_bytes, backups_bytes, free_bytes) "
              "VALUES (datetime('now'), ?, 0, 0, ?)", (110 * mb, 10 * 1024 * mb))
    c.commit()
    c.close()
    out = platform_monitor._size_trend(db)
    assert out["source"] == "daily" and out["points"][-1]["db_mb"] == 110.0
    assert out["growth_mb_per_day"] == 5.0 and out["days_to_full"]


# ── the operator's weekly digest reads the console's real record keys (D #35 meets C) ──

def test_the_weekly_digest_reads_churn_risk_and_the_next_onboarding_step(db, monkeypatch):
    import admin_ops
    import emails
    monkeypatch.setattr(admin_ops, "overview", lambda: {"kpis": {"clients": 2}, "issues": []})
    monkeypatch.setattr(admin_ops, "clients", lambda: {"clients": [
        {"name": "Risky Grill", "segment": "customer",
         "churn_risk": {"level": "high", "points": 7, "reasons": ["no logins in 20 days"], "scored": True},
         "onboarding": {"complete": True}},
        {"name": "New Bistro", "segment": "customer", "churn_risk": {"level": "n/a"},
         "onboarding": {"complete": False, "steps": [{"label": "Contract signed", "done": True},
                                                     {"label": "Connect Google", "done": False}]}},
        {"name": "Demo Co", "segment": "internal", "churn_risk": {"level": "high", "points": 9, "reasons": ["x"]},
         "onboarding": {"complete": False, "steps": []}}]})
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.update(payload) or
                        emails.SendResult(True, attempts=1))
    ops.send_operator_weekly_digest()
    assert "Risky Grill" in sent["html"] and "no logins in 20 days" in sent["html"]
    assert "New Bistro" in sent["html"] and "Connect Google" in sent["html"]
    assert "Demo Co" not in sent["html"]


# ── a text goes out even when its ledger row cannot be written (D meets E #14) ──

def test_a_page_by_text_is_not_held_up_by_a_locked_database(db, monkeypatch):
    import time
    import requests
    import notify

    class _Resp:
        status_code = 201

        def json(self):
            return {"sid": "SM1", "status": "queued"}
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550001111")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    holder = sqlite3.connect(db, timeout=0)
    holder.execute("BEGIN IMMEDIATE")           # the write lock, held
    try:
        started = time.monotonic()
        assert notify.send_sms("+15125550123", "Cavnar AI: the platform needs you", use_case="alert") is True
        assert time.monotonic() - started < notify.SMS_LOG_BUSY_MS / 1000.0 + 2
    finally:
        holder.rollback()
        holder.close()
    # And a database that cannot be opened at all still lets the text go.
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert notify.send_sms("+15125550124", "Cavnar AI: still paging", use_case="alert") is True


# ── a billed reply that could not be used is re-filed 'unparseable' (G #52) ──
# The sites G listed for the files outside its ownership: each marks the
# message create_with_retry returned before it raises, so the ledger's
# outcome is the reply's, not "ok".

class _Msg:
    stop_reason = "end_turn"

    def __init__(self, text, call_id):
        import types
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self._cavnar_call_id = call_id


_PROFILE = {"name": "Mark Grill", "vibe": "bistro", "neighborhood": "Oak Park", "voice": "warm",
            "known_for": "burgers"}


@pytest.fixture
def marks(monkeypatch):
    got = []
    monkeypatch.setattr(ai_utils, "mark_outcome",
                        lambda m, outcome, reason=None, db_path=None: got.append(
                            (m if isinstance(m, str) else m._cavnar_call_id, outcome, reason)) or True)
    return got


@pytest.mark.parametrize("reply,reason", [
    ("Sorry, I can't write that one.", "newsletter copy was not JSON"),
    ('{"subject": "", "body": ""}', "newsletter copy had no subject or body"),
])
def test_an_unreadable_newsletter_draft_is_marked(monkeypatch, marks, reply, reason):
    import types
    import guest_email
    import marketing
    monkeypatch.setattr(marketing, "get_profile_for_restaurant", lambda rid=None: dict(_PROFILE))
    monkeypatch.setattr(guest_email, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(guest_email, "create_with_retry", lambda *a, **k: _Msg(reply, "c-news"))
    with pytest.raises(ValueError, match="newsletter copy was unreadable"):
        guest_email.draft_newsletter(types.SimpleNamespace(id=9, name="Mark Grill"), goal="Fill Thursday")
    assert marks == [("c-news", "unparseable", reason)]


def test_an_empty_campaign_draft_is_marked_and_not_handed_back(monkeypatch, marks):
    import types
    import guest_marketing
    import marketing
    monkeypatch.setattr(marketing, "get_profile_for_restaurant", lambda rid=None: dict(_PROFILE))
    monkeypatch.setattr(guest_marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(guest_marketing, "create_with_retry", lambda *a, **k: _Msg("   ", "c-sms"))
    with pytest.raises(ValueError, match="came back empty"):
        guest_marketing.draft_campaign_message(types.SimpleNamespace(id=9, name="Mark Grill"))
    assert marks == [("c-sms", "unparseable", "empty campaign copy")]


def test_an_unreadable_calendar_week_is_marked(marks):
    import marketing
    with pytest.raises(ValueError):
        marketing._calendar_ideas("I'd rather not plan this week.", message=_Msg("", "c-week"))
    assert marks == [("c-week", "unparseable", "no usable JSON")]
    # A week that reads is not marked.
    assert marketing._calendar_ideas('[{"day": "Monday", "idea": "x"}]', message=_Msg("", "c-ok")) is not None
    assert len(marks) == 1


def test_an_unreadable_invoice_and_recipe_card_are_marked(monkeypatch, marks):
    import inventory_ledger
    import invoices
    import recipes
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _Msg("not json", "c-ocr"))
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda *a, **k: [])
    with pytest.raises(invoices.InvoiceError):
        invoices.extract(9, b"\x89PNG....", "image/png", client=object())
    with pytest.raises(recipes.RecipePhotoError):
        recipes.extract_from_image(9, b"\x89PNG....", "image/png", client=object())
    assert marks == [("c-ocr", "unparseable", "invoice extraction was not JSON"),
                     ("c-ocr", "unparseable", "recipe card read was not JSON")]


def test_an_unreadable_recipe_draft_is_marked_and_skipped(db, monkeypatch, marks):
    import types
    import inventory_ledger
    import recipes
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: types.SimpleNamespace(
        id=rid, module_inventory=1, menu_notes=""))
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda *a, **k: [
        {"name": "Beef", "unit": "lb"}, {"name": "Bun", "unit": "each"}, {"name": "Cheese", "unit": "oz"}])
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _Msg("no recipe here", "c-draft"))
    out = recipes.draft_missing(9, client=object(), db_path=db, items=[{"id": 1, "name": "Burger"}])
    assert out["drafted"] == 0 and out["skipped"] == 1
    assert marks == [("c-draft", "unparseable", "recipe draft was not JSON")]


def test_an_unreadable_notes_read_marks_the_contexts_last_call(monkeypatch, marks):
    import contextvars
    import time as _time
    import sales_audit_notes_ai as notes_ai
    monkeypatch.setattr(notes_ai, "collect_notes", lambda audit: [
        {"section": "labor", "source": "audit", "text": "GM works the floor.", "in_report": False}])
    monkeypatch.setattr(notes_ai, "notes_fingerprint", lambda audit: "fp")
    monkeypatch.setattr(notes_ai, "_prompt", lambda *a: "prompt")

    def reader(action):
        def call(prompt):
            ai_utils._LAST_CALL.set({"call_id": "c-notes", "restaurant_id": None, "action": action,
                                     "at": _time.time()})
            return "I could not read these notes."
        return call

    def read(action):
        monkeypatch.setattr(notes_ai, "_call_claude", reader(action))
        with pytest.raises(ValueError):
            notes_ai.read_notes({"answers": {}}, {})
    contextvars.copy_context().run(read, "audit_notes_read")
    assert marks == [("c-notes", "unparseable", "the notes reader returned no JSON")]
    # Another call's id is never marked for this one.
    contextvars.copy_context().run(read, "guest_campaign_draft")
    assert len(marks) == 1
