"""marketing_emails_opt_out gates scheduler.py's non-critical automated
sends (onboarding day-2/7/30, monthly summary) — never security/
transactional email. This is a real user-facing setting (Account -> Alerts
-> Email preferences), so a restaurant that opts out must actually stop
receiving these, not just have the toggle look like it worked."""
import datetime

import pytest

import models
import scheduler
from models import create_restaurant, Restaurant, update_restaurant, mark_onboarding_sent


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    models.init_onboarding_emails(db_path=db_path)


def _restaurant(db_path, days_old, opted_out=False, already_sent=None):
    created = (datetime.datetime.now() - datetime.timedelta(days=days_old)).isoformat()
    rid = create_restaurant(
        Restaurant(name="Opt Test Co", owner_email="owner@x.com", created_at=created),
        db_path=db_path
    )
    # Onboarding starts only for a signed client whose welcome email went
    # out (fix round E, #20), and counts days from that welcome.
    update_restaurant(rid, {"contract_status": "signed"}, db_path=db_path)
    welcomed = (datetime.datetime.utcnow() - datetime.timedelta(days=days_old, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status) "
                 "VALUES (?, 'send_welcome_set_password_email', 'owner@x.com', 'Welcome', ?, 'sent')",
                 (rid, welcomed))
    conn.commit()
    conn.close()
    # create_restaurant()'s INSERT only covers a fixed initial column set
    # (matching login_notify's own precedent) — marketing_emails_opt_out
    # is set the same way the real self-serve toggle route does it.
    if opted_out:
        update_restaurant(rid, {"marketing_emails_opt_out": 1}, db_path=db_path)
    for stage in (already_sent or []):
        mark_onboarding_sent(rid, stage, db_path=db_path)
    return rid


def _ok():
    """What a sender returns for an email Resend accepted (fix round E, #16):
    the sequence marks a step sent only on .ok."""
    import emails
    return emails.SendResult(True)


def test_opted_out_restaurant_skips_onboarding_day2(db_path, monkeypatch):
    _restaurant(db_path, days_old=2, opted_out=True)
    sent = []
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime.datetime.now())
    monkeypatch.setattr("emails.send_onboarding_day2", lambda **kw: sent.append(kw) or _ok())
    scheduler.run_onboarding_sequence()
    assert sent == []


def test_opted_in_restaurant_still_gets_onboarding_day2(db_path, monkeypatch):
    _restaurant(db_path, days_old=2, opted_out=False)
    sent = []
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime.datetime.now())
    monkeypatch.setattr("emails.send_onboarding_day2", lambda **kw: sent.append(kw) or _ok())
    scheduler.run_onboarding_sequence()
    assert len(sent) == 1


def test_opted_out_restaurant_skips_onboarding_day7(db_path, monkeypatch):
    # day_2 must already be marked sent, or the day_2 branch (days_since>=2)
    # fires first for a 7-day-old restaurant and day_7 is never reached.
    _restaurant(db_path, days_old=7, opted_out=True, already_sent=["day_2"])
    sent = []
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime.datetime.now())
    monkeypatch.setattr("emails.send_onboarding_day7", lambda **kw: sent.append(kw) or _ok())
    scheduler.run_onboarding_sequence()
    assert sent == []


def test_opted_out_restaurant_skips_onboarding_day30(db_path, monkeypatch):
    _restaurant(db_path, days_old=30, opted_out=True, already_sent=["day_2", "day_7"])
    sent = []
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime.datetime.now())
    monkeypatch.setattr("emails.send_onboarding_day30", lambda **kw: sent.append(kw) or _ok())
    scheduler.run_onboarding_sequence()
    assert sent == []
