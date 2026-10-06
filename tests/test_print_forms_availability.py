"""The branded employee availability sheet (Will, 10/6/26): one letter page
in the Cavnar AI brand, blank or with a restaurant's name on it, opened or
downloaded from the admin console's Customers → Onboarding."""
import io
import os

import pytest
from flask import Flask
from pypdf import PdfReader

import admin_routes
import auth
import auth_routes
import models
import print_forms
import staff_settings
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant

CSRF = "forms-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text(pdf):
    r = PdfReader(io.BytesIO(pdf))
    assert len(r.pages) == 1, "one page"
    return r, r.pages[0].extract_text()


def test_the_sheet_is_one_branded_page_with_the_boxes_the_schedule_reads():
    r, text = _text(print_forms.availability_sheet())
    for s in ("Employee availability", "STAFF FORM", "Restaurant".upper(), "Any time".upper(), "Mornings".upper(),
              "Nights".upper(), "Not available".upper(), "Only between".upper(), "Full time", "Part time",
              "at least", "at most", "ideally", "16–17", "14–15", "I can close", "Food protection manager",
              "Employee signature".upper(), "Entered in Cavnar AI on", "Rev 10/6/26"):
        assert s in text, s
    for day in staff_settings.DAYS:
        assert day in text
    # The three dayparts and "off" on the sheet are the schedule's own choices.
    assert set(staff_settings.DAYPART_CHOICES) == {"any", "morning", "night", "off"}
    fonts = set()
    for res in r.pages[0]["/Resources"]["/Font"].values():
        fonts.add(str(res.get_object()["/BaseFont"]))
    assert any("Clash" in f for f in fonts) and any("Apfel" in f for f in fonts), fonts


def test_a_restaurant_name_is_printed_when_given():
    _r, text = _text(print_forms.availability_sheet("Simple EJ's"))
    assert "Simple EJ's" in text


def test_the_brand_comes_from_the_email_palette_and_the_pdf_fonts_are_truetype():
    src = open(os.path.join(ROOT, "print_forms.py"), encoding="utf-8").read()
    assert "from emails import BRAND" in src and "#c84b2f" not in src
    for _name, f in print_forms._FACES:
        with open(os.path.join(print_forms.FONTS, f), "rb") as fh:
            assert fh.read(4) == b"\x00\x01\x00\x00", f + " must be TrueType outlines (reportlab embeds no CFF)"


@pytest.fixture
def client(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, auth_routes, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(admin_routes, "get_restaurant", lambda rid, **k: models.get_restaurant(rid, db_path=db_path))
    init_auth(db_path=db_path)
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    app.register_blueprint(auth_routes.auth_bp)
    home = create_restaurant(Restaurant(name="HQ", owner_email="hq@x.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    owner = create_user(home, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    c.owner_token = create_session(owner, password_verified_at=True, db_path=db_path)
    c.rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.test"), db_path=db_path)
    return c


def test_the_admin_opens_or_downloads_it(client):
    r = client.get("/admin/forms/availability.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert "attachment" not in r.headers.get("Content-Disposition", ""), "opens in the browser to print"
    r = client.get("/admin/forms/availability.pdf?restaurant_id=%d&download=1" % client.rid)
    assert r.status_code == 200
    assert 'attachment; filename=employee-availability-simple-ej-s.pdf' in r.headers["Content-Disposition"]
    assert "Simple EJ's" in _text(r.data)[1]
    assert client.get("/admin/forms/availability.pdf?restaurant_id=99999").status_code == 404


def test_an_owner_cannot_reach_it(client):
    client.set_cookie("session_token", client.owner_token)
    r = client.get("/admin/forms/availability.pdf")
    assert r.status_code in (302, 401, 403, 404)
    assert r.mimetype != "application/pdf"


def test_the_console_offers_it_on_onboarding():
    html = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    fn = html[html.index("async function clientForms(){"):html.index("async function onboarding(){")]
    assert "Employee availability sheet" in fn and "'/admin/forms/availability.pdf'" in fn
    assert "Open to print" in fn and "Download PDF" in fn and "download=1" in fn
    onb = html[html.index("async function onboarding(){"):]
    assert "const forms = await clientForms();" in onb[:600]
