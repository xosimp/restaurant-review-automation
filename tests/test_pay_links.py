"""Payment links that never expire (MOD-BIL-5): the email links to /pay,
which mints a fresh Checkout Session on each click."""
import pytest
from flask import Flask

import emails
import models
import webhook_routes
from models import Restaurant, create_restaurant


@pytest.fixture
def client(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app.test_client()


def test_a_pay_link_mints_a_fresh_checkout_for_its_restaurant(client, db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Pay Co", owner_email="p@x.test", module_reviews=1, module_labor=0,
                                       module_inventory=0, module_marketing=0,
                                       billing_status="pending"), db_path=db_path)
    made = []
    monkeypatch.setattr(emails, "create_stripe_checkout",
                        lambda n, email, name, period="monthly", **k: made.append((n, email, period)) or "https://checkout.stripe.com/x")
    link = emails.pay_link(rid, "annual")
    path = link.split("://", 1)[1].split("/", 1)[1]
    r = client.get("/" + path)
    assert r.status_code == 302 and r.headers["Location"] == "https://checkout.stripe.com/x"
    assert made == [(1, "p@x.test", "annual")]


def test_a_tampered_pay_link_is_refused(client, db_path):
    rid = create_restaurant(Restaurant(name="Pay Co", owner_email="p@x.test"), db_path=db_path)
    assert client.get(f"/pay/{rid}.deadbeef/monthly").status_code == 404


def test_an_already_paying_restaurant_is_told_it_is_set(client, db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Paid Co", owner_email="p@x.test", billing_status="active"),
                            db_path=db_path)
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda *a, **k: pytest.fail("no new checkout"))
    path = emails.pay_link(rid).split("://", 1)[1].split("/", 1)[1]
    r = client.get("/" + path)
    assert r.status_code == 200 and "all set" in r.get_data(as_text=True)
