"""Phone numbers read "(334) 568-9292" everywhere (Will, 9/29/26): one rule
for display (auth.display_phone, the |format_phone filter, fmtPhone on the
web, PhoneFormat on iOS) and owner phones saved that way."""
import os

import models
from auth import display_phone
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_us_numbers_format_however_stored_and_others_pass_through():
    for raw in ("+13345689292", "3345689292", "1-334-568-9292", "(334)568 9292", "334.568.9292"):
        assert display_phone(raw) == "(334) 568-9292", raw
    assert display_phone("+44 20 7946 0958") == "+44 20 7946 0958"
    assert display_phone("555-1234") == "555-1234"
    assert display_phone(None) == ""


def test_owner_phone_is_saved_formatted_on_create_and_update(db_path):
    rid = create_restaurant(Restaurant(name="Phone Co", owner_email="p@x.test", owner_phone="3345689292"), db_path=db_path)
    assert get_restaurant(rid, db_path=db_path).owner_phone == "(334) 568-9292"
    update_restaurant(rid, {"owner_phone": "+1 312 555 0100"}, db_path=db_path)
    assert get_restaurant(rid, db_path=db_path).owner_phone == "(312) 555-0100"


def test_every_page_that_shows_a_phone_loads_the_formatter():
    for page in ("dashboard", "admin", "client_settings", "staff_login", "guest_optin"):
        assert '/static/cavnar-phone.js' in open(os.path.join(ROOT, "templates", page + ".html")).read(), page
    js = open(os.path.join(ROOT, "static", "cavnar-phone.js")).read()
    assert "window.fmtPhone = fmtPhone" in js and "=>" not in js and "let " not in js and "const " not in js
    admin = open(os.path.join(ROOT, "templates", "admin.html")).read()
    assert '<input id="r-phone" type="tel"' in admin     # the new-client form formats as it is typed
    assert "esc(fmtPhone(c.owner_phone))" in admin
