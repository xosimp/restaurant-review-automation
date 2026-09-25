"""The welcome email Erik (the first live client) receives after signing
(9/25/26): greets the owner by first name, puts the login on a visible card
with a Sign in button, takes every colour from emails.BRAND, speaks miles,
and — from the DocuSign webhook, its real sender — carries the "What I can
already see" first look, which needs the Place ID the webhook used to leave
out of its SELECT."""
import os
import re

import emails

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _render(monkeypatch, **kw):
    got = {}
    monkeypatch.setattr(emails, "deliver", lambda email_type=None, payload=None, **k: got.update(payload or {}))
    args = dict(to_email="o@x.test", restaurant_name="Simple EJ's", username="erik", password="Xk3v9Qa_2pLm",
                module_reviews=1, module_labor=1)
    args.update(kw)
    emails.send_welcome_email(**args)
    return got


def test_greets_by_first_name_and_falls_back_cleanly(monkeypatch):
    assert "Hi Erik —" in _render(monkeypatch, owner_name="Erik Johnson")["html"]
    assert "Hi —" in _render(monkeypatch)["html"]


def test_login_sits_on_a_white_card_with_a_sign_in_button(monkeypatch):
    html = _render(monkeypatch)["html"]
    card = html[html.index("Your login details") - 400:html.index("Your login details")]
    assert "background:%s" % emails.BRAND["card"] in card
    assert "Sign in to your dashboard" in html and "Xk3v9Qa_2pLm" in html


def test_every_colour_is_a_brand_token(monkeypatch):
    html = _render(monkeypatch)["html"]
    body = html[html.index("Restaurant Intelligence Dashboard") - 2000:]
    allowed = {v.lower() for v in emails.BRAND.values()}
    used = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", body)}
    assert used <= allowed, used - allowed


def test_the_first_look_speaks_miles_and_american_english():
    import first_look
    look = {"rating": 4.6, "review_count": 208,
            "neighbourhood": {"avg_rating": 4.1, "count": 5, "matched": 5, "radius_km": 3.8,
                              "effective_reviews": 900}}
    text = " ".join(first_look.lines(look))
    assert "km" not in text and "neighbourhood" not in text
    assert "2.4 miles" in text


def test_the_signing_webhook_reads_the_place_id_and_owner_name():
    src = open(os.path.join(ROOT, "webhook_routes.py"), encoding="utf-8").read()
    sel = src[src.index('"""SELECT r.id, r.name, r.owner_email'):]
    sel = sel[:sel.index("FROM restaurants r")]
    assert "r.google_place_id" in sel and "r.owner_name" in sel
    call = src[src.index("send_welcome_email(\n", src.index("A fresh temporary password")):]
    call = call[:call.index(")\n")]
    assert "google_place_id=r.get(\"google_place_id\")" in call and "owner_name=r.get(\"owner_name\")" in call
