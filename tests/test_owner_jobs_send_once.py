"""The owner-facing jobs send once, however many hourly ticks see their
window (audit #25 A01-11, A03-11, A07-16).

The loop attempts the weekly digest, the monthly summary and the onboarding
sequence every hour, and each restaurant's own local hour gates it
(scheduler.local_due with a claim per restaurant per day). This was pinned as
source text — `'claim_key="weekly_digest"' in inspect.getsource(...)` — which
a refactor could keep while breaking the behaviour (the claim in a branch
never taken, a second sender beside it). These run each job, really: the
real local_due and the real claims in a test database, only the clock
pinned and the sender recorded; two ticks in the window, then a later one,
then one more after a transient failure gave the claim back.
"""
import sys
from datetime import datetime

import pytest

import emails
import models
import ops
import scheduler
import time_utils
from models import Restaurant, create_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    models.init_onboarding_emails(db_path=db_path)
    return db_path


class _Clock:
    """The restaurant's wall clock, moved by the test between ticks."""

    def __init__(self, monkeypatch, when):
        self.now = when
        monkeypatch.setattr(time_utils, "restaurant_now",
                            lambda r=None, naive=False: self.now if naive else
                            self.now.replace(tzinfo=time_utils.restaurant_tz(r)))
        monkeypatch.setattr(scheduler, "_chi_now", lambda: self.now)


def _restaurant(db, **kw):
    kw.setdefault("name", "Once Grill")
    kw.setdefault("owner_email", "owner@once.test")
    kw.setdefault("timezone", "America/Chicago")
    kw.setdefault("billing_status", "active")
    return create_restaurant(Restaurant(**kw), db_path=db)


def _ok():
    return emails.SendResult(True, message_id="m-1")


def _transient():
    return emails.SendResult(False, error="HTTP 503", status_code=503, attempts=3)


# ── the monthly summary ─────────────────────────────────────────────────────

def test_the_monthly_summary_goes_once_however_many_ticks_see_the_first(db, monkeypatch):
    _restaurant(db)
    clock = _Clock(monkeypatch, datetime(2026, 10, 1, 9, 5))
    sent = []
    monkeypatch.setattr(emails, "send_monthly_summary_email", lambda **kw: sent.append(kw["restaurant_id"]) or _ok())
    monkeypatch.setattr(scheduler, "_push_month_ready", lambda r: None)
    first = scheduler.run_monthly_summaries()
    second = scheduler.run_monthly_summaries()            # the same hour, another tick
    clock.now = datetime(2026, 10, 1, 10, 5)
    third = scheduler.run_monthly_summaries()             # the next hour, still in the window
    assert len(sent) == 1
    assert (first["ok"], second["ok"], third["ok"]) == (1, 0, 0)
    clock.now = datetime(2026, 10, 2, 9, 5)
    assert scheduler.run_monthly_summaries()["ok"] == 0 and len(sent) == 1     # the 2nd is not the 1st


def test_a_transient_monthly_failure_gives_the_day_back_and_the_next_tick_sends_once(db, monkeypatch):
    _restaurant(db)
    clock = _Clock(monkeypatch, datetime(2026, 10, 1, 9, 5))
    answers = [_transient(), _ok(), _ok()]
    sent = []

    def send(**kw):
        sent.append(kw["restaurant_id"])
        return answers.pop(0)
    monkeypatch.setattr(emails, "send_monthly_summary_email", send)
    monkeypatch.setattr(scheduler, "_push_month_ready", lambda r: None)
    assert scheduler.run_monthly_summaries()["failed"] == 1
    clock.now = datetime(2026, 10, 1, 10, 5)
    assert scheduler.run_monthly_summaries()["ok"] == 1
    assert scheduler.run_monthly_summaries()["ok"] == 0
    assert len(sent) == 2                                  # the failure, then exactly one delivery


# ── the weekly digest ───────────────────────────────────────────────────────

def _digest_world(db, monkeypatch, deliver):
    import auth
    import reporter
    rid = _restaurant(db, module_reviews=1, digest_day="monday", digest_enabled=1)
    auth.create_user(rid, "owner", "owner@once.test", "Once-pass-2026", db_path=db)
    monkeypatch.setattr(reporter, "build_report_from_db", lambda rid, name, days=7: {"rid": rid})
    monkeypatch.setattr(reporter, "digest_has_data", lambda r, rep: True)
    monkeypatch.setattr(reporter, "render_digest", lambda rep, name, **kw: {"html": "<p>week</p>", "subject": "Week"})
    monkeypatch.setattr(emails, "digest_preheader", lambda rep, r: "")
    monkeypatch.setattr(emails, "deliver", deliver)
    return rid


def test_the_weekly_digest_goes_once_however_many_ticks_see_the_window(db, monkeypatch):
    clock = _Clock(monkeypatch, datetime(2026, 10, 5, 9, 5))          # a Monday
    sent = []
    _digest_world(db, monkeypatch, lambda **kw: sent.append(kw["payload"]["to"]) or _ok())
    counts = [scheduler.run_weekly_digests()["ok"], scheduler.run_weekly_digests()["ok"]]
    clock.now = datetime(2026, 10, 5, 11, 5)
    counts.append(scheduler.run_weekly_digests()["ok"])
    assert sent == [["owner@once.test"]] and counts == [1, 0, 0]


def test_a_transient_digest_failure_is_retried_by_the_next_tick_and_sent_once(db, monkeypatch):
    clock = _Clock(monkeypatch, datetime(2026, 10, 5, 9, 5))
    answers = [_transient(), _ok(), _ok()]
    sent = []

    def deliver(**kw):
        sent.append(kw["payload"]["to"])
        return answers.pop(0)
    _digest_world(db, monkeypatch, deliver)
    assert scheduler.run_weekly_digests()["failed"] == 1
    clock.now = datetime(2026, 10, 5, 10, 5)
    assert scheduler.run_weekly_digests()["ok"] == 1
    assert scheduler.run_weekly_digests()["ok"] == 0
    assert len(sent) == 2


# ── the onboarding sequence ─────────────────────────────────────────────────

def _onboarding_world(db, monkeypatch, send):
    rid = _restaurant(db, contract_status="signed")
    monkeypatch.setattr(scheduler, "onboarding_eligible", lambda r: (True, None))
    monkeypatch.setattr(scheduler, "_onboarding_days", lambda r: 3)            # inside day 2's window
    monkeypatch.setattr(emails, "send_onboarding_day2", send)
    return rid


def test_the_onboarding_step_goes_once_however_many_ticks_see_ten_am(db, monkeypatch):
    clock = _Clock(monkeypatch, datetime(2026, 10, 6, 10, 5))
    sent = []
    rid = _onboarding_world(db, monkeypatch, lambda **kw: sent.append(kw["restaurant_id"]) or _ok())
    scheduler.run_onboarding_sequence(local_hour=10)
    scheduler.run_onboarding_sequence(local_hour=10)
    clock.now = datetime(2026, 10, 6, 12, 5)
    scheduler.run_onboarding_sequence(local_hour=10)
    clock.now = datetime(2026, 10, 7, 10, 5)                                  # the next day: day 2 is done
    scheduler.run_onboarding_sequence(local_hour=10)
    assert sent == [rid]


def test_a_transient_onboarding_failure_is_retried_within_the_day_and_sent_once(db, monkeypatch):
    clock = _Clock(monkeypatch, datetime(2026, 10, 6, 10, 5))
    answers = [_transient(), _ok(), _ok()]
    sent = []

    def send(**kw):
        sent.append(kw["restaurant_id"])
        return answers.pop(0)
    _onboarding_world(db, monkeypatch, send)
    scheduler.run_onboarding_sequence(local_hour=10)          # 503: nothing marked, the day given back
    clock.now = datetime(2026, 10, 6, 11, 5)
    scheduler.run_onboarding_sequence(local_hour=10)          # the next tick delivers
    scheduler.run_onboarding_sequence(local_hour=10)          # and that is the end of it
    assert len(sent) == 2
