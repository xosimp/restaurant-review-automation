"""Fix round H — the billing emails (the section appended to emails.py).

Each returns deliver()'s SendResult (never None), passes restaurant_id so
the client's email history shows it, takes every colour from emails.BRAND,
and says "Cavnar AI", never a bare "Cavnar". The dunning email changes with
the attempt (#25); the welcome carries a set-password link and no password
(#12); the reminder quotes pricing.py (#26); the receipt is per invoice
(#155).
"""
import re

import pytest

import emails


@pytest.fixture
def sent(monkeypatch):
    box = []

    def fake(payload=None, restaurant_id=None, email_type=None, **k):
        box.append({"payload": payload, "restaurant_id": restaurant_id, "email_type": email_type})
        return emails.SendResult(True, message_id="m")
    monkeypatch.setattr(emails, "deliver", fake)
    return box


def _brand_only(html):
    allowed = {v.lower() for v in emails.BRAND.values()}
    used = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", html)}
    return used - allowed


def _no_bare_cavnar(text):
    return not re.search(r"Cavnar(?! AI)(?!\.ai)(?!@)", text.replace("Will Cavnar", ""))


@pytest.mark.parametrize("attempt,title,phrase", [
    (1, "didn't go through", "keeps running"),
    (2, "declined again", "second time"),
    (3, "update your card", "third decline"),
])
def test_dunning_changes_with_the_attempt(sent, attempt, title, phrase):
    res = emails.send_dunning_email("o@x.test", "Joe's <Pizza>", 349.0, attempt,
                                    pay_url="https://dash/pay/1.s/invoice", card_url="https://dash/pay/1.s/card",
                                    next_attempt="10/3/26", owner_name="Joe Smith", restaurant_id=7)
    assert res.ok
    (s,) = sent
    html = s["payload"]["html"]
    assert title.lower() in s["payload"]["subject"].lower() and phrase in html
    assert s["restaurant_id"] == 7 and s["email_type"] == "send_dunning_email"
    assert "Joe&#x27;s &lt;Pizza&gt;" in html and "<Pizza>" not in html          # owner text escaped
    assert "https://dash/pay/1.s/invoice" in html and "https://dash/pay/1.s/card" in html
    assert "$349.00" in html and "10/3/26" in html and "Hi Joe" in html
    assert not _brand_only(html) and _no_bare_cavnar(html)


def test_the_last_dunning_email_says_when_stripe_will_not_retry(sent):
    emails.send_dunning_email("o@x.test", "R", 349.0, 3, "https://p", "https://c", next_attempt="")
    assert "won't try again" in sent[0]["payload"]["html"]


def test_the_welcome_has_a_set_password_link_and_no_password(sent, db_path, monkeypatch):
    # One welcome (integration wave): the post-signing welcome is
    # emails.send_welcome_with_set_password_link — the email resend-welcome
    # and checkout provisioning send too. H's own template is gone.
    import models
    from auth import create_user, init_auth
    init_auth(db_path=db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="Simple EJ's", owner_email="o@x.test",
                                                     owner_name="Erik J"), db_path=db_path)
    models.update_restaurant(rid, {"module_reviews": 1, "module_labor": 1}, db_path=db_path)
    uid = create_user(rid, "erik", "o@x.test", "Owner-typed-2026", db_path=db_path)
    res = emails.send_welcome_with_set_password_link(user_id=uid, restaurant_id=rid, db_path=db_path)
    assert res.ok
    assert not hasattr(emails, "send_signed_welcome_email")
    html = sent[0]["payload"]["html"]
    assert "Set your password" in html and "/reset-password/" in html
    assert "Temporary password" not in html and "Owner-typed-2026" not in html
    assert f"{emails.SET_PASSWORD_LINK_DAYS} days" in html and "Forgot password" in html
    assert sent[0]["restaurant_id"] == rid and sent[0]["email_type"] == "send_welcome_set_password_email"
    assert not _brand_only(html) and _no_bare_cavnar(html)


def test_the_pay_reminder_quotes_the_one_price_list(sent):
    import pricing
    emails.send_pay_reminder_email("o@x.test", "R", 2, "https://m", "https://a", day=5, restaurant_id=4)
    html = sent[0]["payload"]["html"]
    plan = pricing.plan_for(2)
    for figure in (pricing.money(plan["setup"]), pricing.money(plan["monthly"]), pricing.money(plan["annual"])):
        assert figure in html
    assert "https://m" in html and "https://a" in html and sent[0]["restaurant_id"] == 4
    assert not _brand_only(html)


def test_the_receipt_and_the_card_update_email(sent):
    emails.send_payment_receipt_email("o@x.test", "R", 750.0, "9/28/26", "Setup fee",
                                      receipt_url="https://invoice.stripe.com/i/x", restaurant_id=5)
    emails.send_card_update_email("o@x.test", "R", "https://c", pay_url="https://p", amount_due=349.0,
                                  restaurant_id=5)
    rec, card = sent
    assert "$750.00" in rec["payload"]["html"] and "9/28/26" in rec["payload"]["html"]
    assert rec["email_type"] == "send_payment_receipt_email" and rec["restaurant_id"] == 5
    assert "https://c" in card["payload"]["html"] and "$349.00" in card["payload"]["html"]
    assert not _brand_only(rec["payload"]["html"] + card["payload"]["html"])
