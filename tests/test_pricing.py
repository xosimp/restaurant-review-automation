"""pricing.py is the only place prices live; the contract tabs, Stripe
amounts, payment email and sales audit must all read from it."""
import pricing


def test_plans_match_the_public_pricing_page():
    s = pricing.plan_for(1)
    assert (s["setup"], s["monthly"], s["annual"]) == (750, 349, 3490) and s["published"]
    f = pricing.plan_for(4)
    assert (f["setup"], f["monthly"], f["annual"]) == (3000, 1199, 11990) and f["plan"] == "full"
    # Every tier the page publishes, in the page's own numbers. 2 and 3 used
    # to be computed as N x Starter here ($698/$1,047) while pricing.html's
    # FAQ advertised $649/$899 — a client would have signed at one price and
    # been billed the other.
    assert {n: (t["setup"], t["monthly"], t["annual"]) for n, t in pricing.TIERS.items()} == {
        1: (750, 349, 3490),
        2: (1500, 649, 6490),
        3: (2250, 899, 8990),
        4: (3000, 1199, 11990),
    }
    for n in (1, 2, 3, 4):
        p = pricing.plan_for(n)
        assert p["published"], f"{n} modules is on the public page — it must not be flagged unpublished"
        assert p["annual"] == p["monthly"] * 10, f"{n} modules: annual must be ten months (two free)"
        assert p["setup"] == 750 * n, f"{n} modules: setup is $750 per module"
    # The ladder is hand-picked round numbers, not a formula, so don't pin a
    # smooth curve on it — 3 modules works out at $299.67 each and 4 at
    # $299.75, an eight-cent artifact of $899 and $1,199 being nice prices.
    # What must hold: buying more modules never costs less in total, and
    # there is a real volume discount by the top of the ladder.
    monthly = [pricing.plan_for(n)["monthly"] for n in (1, 2, 3, 4)]
    assert monthly == sorted(monthly), f"more modules costs less: {monthly}"
    assert pricing.plan_for(4)["monthly"] / 4 < pricing.plan_for(1)["monthly"], "no volume discount at all"
    assert pricing.plan_for(4)["monthly"] < pricing.plan_for(1)["monthly"] * 4, "the full system costs more than 4 singles"
    # Above the top tier you get the Full System, not a bigger bill.
    assert pricing.plan_for(9)["monthly"] == 1199 and pricing.plan_for(9)["modules"] == 4
    assert pricing.plan_for(0)["setup"] == 0
    assert pricing.annual_saving(1) == 349 * 12 - 3490 and pricing.annual_saving(4) == 1199 * 12 - 11990
    assert pricing.annual_saving(2) == 649 * 2 and pricing.annual_saving(3) == 899 * 2


def test_the_mrr_table_and_the_billing_table_cannot_drift():
    """admin_ops used to keep its own {1: 349, 2: 649, ...} literal, which is
    how the disagreement survived: the MRR dashboard and the website agreed
    with each other and disagreed with the thing that sends the contract."""
    import admin_ops
    assert admin_ops.MONTHLY_BY_MODULES == {n: t["monthly"] for n, t in pricing.TIERS.items()}


def test_stripe_checkout_uses_pricing(monkeypatch):
    import emails
    captured = {}

    class _Price:
        @staticmethod
        def create(**kw):
            captured.setdefault("prices", []).append(kw)
            return type("P", (), {"id": "price_x"})()

    class _Product:
        @staticmethod
        def search(**kw):
            return type("R", (), {"data": []})()

        @staticmethod
        def create(**kw):
            return type("P", (), {"id": "prod_x"})()

    class _Session:
        @staticmethod
        def create(**kw):
            captured["session"] = kw
            return type("S", (), {"url": "https://checkout.stripe.com/x"})()

    import types
    fake = types.SimpleNamespace(api_key=None, Price=_Price, Product=_Product, checkout=types.SimpleNamespace(Session=_Session))
    monkeypatch.setitem(__import__("sys").modules, "stripe", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    url = emails.create_stripe_checkout(4, "o@example.com", "R", "annual")
    assert url and [p["unit_amount"] for p in captured["prices"]] == [300000, 1199000]
    # setup at checkout, retainer on day 31 — for annual too
    assert captured["session"]["subscription_data"]["trial_period_days"] == pricing.RETAINER_START_DAYS == 30
    assert "$3,000 setup today" in captured["session"]["custom_text"]["submit"]["message"]
    assert "$11,990" in captured["session"]["custom_text"]["submit"]["message"]
    captured.clear()
    emails.create_stripe_checkout(1, "o@example.com", "R", "monthly")
    assert [p["unit_amount"] for p in captured["prices"]] == [75000, 34900]
    assert captured["session"]["subscription_data"]["trial_period_days"] == 30
    assert "$349" in captured["session"]["custom_text"]["submit"]["message"]


def test_docusign_tabs_and_admin_role(monkeypatch):
    import docusign_helper as d
    monkeypatch.setattr(d, "INTEGRATION_KEY", "k"); monkeypatch.setattr(d, "USER_ID", "u"); monkeypatch.setattr(d, "ACCOUNT_ID", "a")
    monkeypatch.setattr(d, "TEMPLATE_ID", "t"); monkeypatch.setattr(d, "PRIVATE_KEY", "p")
    monkeypatch.setattr(d, "get_access_token", lambda: "tok")
    sent = {}

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"envelopeId": "env1", "status": "sent"}

    import requests
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None: sent.update(body=json) or _Resp())
    r = d.send_contract("erik@example.com", "Erik", "Simple EJ's", 4, "all")
    assert r["ok"] and r["envelope_id"] == "env1"
    roles = {x["roleName"]: x for x in sent["body"]["templateRoles"]}
    assert set(roles) == {"Admin", "Client"}
    tabs = {t["tabLabel"]: t["value"] for t in roles["Client"]["tabs"]["textTabs"]}
    assert tabs["setup_fee"] == "$3,000" and tabs["monthly_fee"] == "$1,199 / month" and tabs["annual_fee"] == "$11,990 / year"
    assert tabs["modules"] == "all" and tabs["restaurant_name"] == "Simple EJ's" and tabs["owner_email"] == "erik@example.com"
