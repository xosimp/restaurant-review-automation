"""pricing.py is the only place prices live; the contract tabs, Stripe
amounts, payment email and sales audit must all read from it."""
import pricing


def test_plans_match_the_public_pricing_page():
    s = pricing.plan_for(1)
    assert (s["setup"], s["monthly"], s["annual"]) == (750, 349, 3490) and s["published"]
    f = pricing.plan_for(4)
    assert (f["setup"], f["monthly"], f["annual"]) == (3000, 1199, 11990) and f["plan"] == "full"
    two = pricing.plan_for(2)
    assert two["annual"] == 6980 and not two["published"]
    assert pricing.plan_for(0)["setup"] == 0
    assert pricing.annual_saving(1) == 349 * 12 - 3490 and pricing.annual_saving(4) == 1199 * 12 - 11990


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
    captured.clear()
    emails.create_stripe_checkout(1, "o@example.com", "R", "monthly")
    assert [p["unit_amount"] for p in captured["prices"]] == [75000, 34900]


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
    assert tabs["setup_fee"] == "$3,000" and tabs["monthly_fee"] == "$1,199/mo"
