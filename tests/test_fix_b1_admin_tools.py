"""Fix round B1 — admin tools that lost data or held request threads
(#23, #73, #142, #152, #153).

- Menu extraction runs as a bounded background job and hands the text back
  to review; nothing is saved until the admin saves the settings.
- New client: a validated timezone, validated input, and the Places and
  DocuSign calls off the request thread (and never sent from a local backend).
- Branding: a GET for the current values; blanks never wipe; colours and
  logos validated; a stale form is a 409.
- Staff notes add rather than replace, removals say what went; ingredient
  and recipe numbers are refused rather than 500ing or storing NaN;
  alert contacts are capped and scoped.
"""
import io
import json
import os
import time

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import competitor
import models
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, restaurant_version, update_restaurant

CSRF = "b1-tools-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.secret_key = "b1-tools"
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    return flask_app


@pytest.fixture
def admin(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


@pytest.fixture
def inline_jobs(monkeypatch):
    """Run admin jobs on the calling thread, so a test reads their result."""
    def run(job_id, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            admin_routes._ops.finish_async_job(job_id, "error", {"ok": False, "error": str(e)})
        return None
    monkeypatch.setattr(admin_routes, "_submit_admin_job", run)


def _rid(db_path, name="Client Grill", **kw):
    """create_restaurant writes only its own columns (no menu or brand
    fields), so the rest go through update_restaurant."""
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _post(c, url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _job(c, kind, job_id):
    for _ in range(200):
        out = c.get(f"/admin/api/{kind}/{job_id}").get_json()
        if out.get("status") != "pending":
            return out
        time.sleep(0.05)
    raise AssertionError("job never finished")


# ── #142, #153: menu extraction is a job, and saves nothing ─────────────────

def test_a_pdf_extraction_comes_back_to_review_and_saves_nothing(admin, db_path, inline_jobs, monkeypatch):
    rid = _rid(db_path, menu_notes="Hand-curated: short rib pasta")
    monkeypatch.setattr(competitor, "fetch_menu_from_pdf_bytes",
                        lambda b, name, restaurant_id=None: "Extracted: pizza, pasta")
    r = _post(admin, f"/admin/upload-menu-pdf/{rid}", data={"pdf": (io.BytesIO(b"%PDF-1.4 menu"), "m.pdf")},
              content_type="multipart/form-data")
    body = r.get_json()
    assert body["ok"] is True and body["status"] == "pending"
    done = _job(admin, "menu-extract", body["job_id"])
    assert done["ok"] is True and done["menu_notes"] == "Extracted: pizza, pasta" and done["source"] == "pdf"
    assert get_restaurant(rid).menu_notes == "Hand-curated: short rib pasta", "review first, save with the settings"


def test_a_url_extraction_saves_neither_the_notes_nor_the_url(admin, db_path, inline_jobs, monkeypatch):
    rid = _rid(db_path, menu_notes="Keep me", menu_url="https://old.example/menu")
    monkeypatch.setattr(competitor, "fetch_menu_from_url", lambda url, restaurant_id=None: "From the web")
    r = _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://new.example/menu"})
    done = _job(admin, "menu-extract", r.get_json()["job_id"])
    assert done["menu_notes"] == "From the web" and done["menu_url"] == "https://new.example/menu"
    row = get_restaurant(rid)
    assert (row.menu_notes, row.menu_url) == ("Keep me", "https://old.example/menu")


def test_a_places_refresh_merges_with_the_saved_notes_but_saves_nothing(admin, db_path, inline_jobs, monkeypatch):
    rid = _rid(db_path, google_place_id="ChIJx", menu_notes="Owner's specials")
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places",
                        lambda pid, restaurant_id=None: "Places says: tacos, burritos, churros and more")
    done = _job(admin, "menu-extract", _post(admin, f"/admin/refresh-menu-notes/{rid}").get_json()["job_id"])
    assert done["ok"] is True and "Owner's specials" in done["menu_notes"] and "tacos" in done["menu_notes"]
    assert get_restaurant(rid).menu_notes == "Owner's specials"


def test_menu_extraction_input_is_checked_before_a_job_starts(admin, db_path, inline_jobs):
    rid = _rid(db_path)
    not_pdf = _post(admin, f"/admin/upload-menu-pdf/{rid}", data={"pdf": (io.BytesIO(b"hello"), "m.pdf")},
                    content_type="multipart/form-data")
    assert not_pdf.status_code == 400
    bad_url = _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "javascript:alert(1)"})
    assert bad_url.status_code == 400
    no_place = _post(admin, f"/admin/refresh-menu-notes/{rid}")
    assert no_place.status_code == 400


def test_a_second_extraction_while_one_runs_is_refused(admin, db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: None)   # never runs
    first = _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://a.example/menu"})
    second = _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://b.example/menu"})
    assert first.get_json()["ok"] is True
    assert second.status_code == 409 and second.get_json()["job_id"] == first.get_json()["job_id"]


def test_the_admin_job_pool_is_bounded(admin, db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(admin_routes, "ADMIN_JOB_QUEUE_MAX", 0)
    r = _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://a.example/menu"})
    assert r.status_code == 503 and "busy" in r.get_json()["error"]
    assert "ThreadPoolExecutor(max_workers=ADMIN_JOB_WORKERS" in open(os.path.join(ROOT, "admin_routes.py")).read()


def test_a_job_on_the_real_pool_finishes_and_a_raising_one_is_an_error(admin, db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(competitor, "fetch_menu_from_url", lambda url, restaurant_id=None: "Real pool text")
    ok = _job(admin, "menu-extract",
              _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://a.example/m"}).get_json()["job_id"])
    assert ok["menu_notes"] == "Real pool text"
    monkeypatch.setattr(competitor, "fetch_menu_from_url",
                        lambda url, restaurant_id=None: (_ for _ in ()).throw(RuntimeError("upstream down")))
    bad = _job(admin, "menu-extract",
               _post(admin, f"/admin/fetch-menu-from-url/{rid}", json={"url": "https://a.example/m"}).get_json()["job_id"])
    assert bad["ok"] is False and bad["status"] == "error"


# ── #152, #153: New client ─────────────────────────────────────────────────

NEW = {"restaurant_name": "Pacific Grill", "owner_email": "owner@pacific.test", "username": "pacificgrill",
       "password": "long-enough-pass-1", "module_reviews": 1, "module_marketing": 1, "google_place_id": "ChIJpac"}


def test_new_client_stores_the_timezone_it_was_given(admin, db_path, inline_jobs, monkeypatch):
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places", lambda pid, restaurant_id=None: None)
    body = _post(admin, "/admin/create-client", json=dict(NEW, timezone="America/Los_Angeles")).get_json()
    assert body["ok"] is True, body
    assert body["timezone"] == "America/Los_Angeles" and body["timezone_defaulted"] is False
    assert get_restaurant(body["restaurant_id"]).timezone == "America/Los_Angeles"


def test_new_client_without_a_timezone_says_it_defaulted(admin, db_path, inline_jobs, monkeypatch):
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places", lambda pid, restaurant_id=None: None)
    body = _post(admin, "/admin/create-client", json=dict(NEW)).get_json()
    assert body["ok"] is True and body["timezone_defaulted"] is True
    assert get_restaurant(body["restaurant_id"]).timezone == "America/Chicago"


@pytest.mark.parametrize("change,needle", [
    ({"timezone": "Mars/Olympus"}, "timezone"),
    ({"timezone": "Europe/Paris"}, "timezone"),
    ({"owner_email": "not an email"}, "Owner email"),
    ({"username": "has space"}, "username"),
    ({"restaurant_name": ""}, "Restaurant name"),
    ({"password": None}, "Temporary password"),
    ({"google_place_id": "ChIJ with spaces"}, "Place ID"),
    ({"owner_phone": 5551234}, "Owner phone"),
])
def test_new_client_input_is_validated_before_anything_is_created(admin, db_path, change, needle):
    body = dict(NEW, **change)
    r = _post(admin, "/admin/create-client", json=body)
    assert r.status_code == 400 and needle.lower() in r.get_json()["error"].lower(), r.get_json()
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM restaurants WHERE owner_email LIKE '%pacific%'").fetchone()[0]
    conn.close()
    assert n == 0


def test_new_client_refuses_a_non_object_body(admin):
    r = _post(admin, "/admin/create-client", data="[1]", content_type="application/json")
    assert r.status_code == 400


def test_a_username_that_differs_only_by_case_is_a_duplicate(admin, db_path, inline_jobs, monkeypatch):
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places", lambda pid, restaurant_id=None: None)
    assert _post(admin, "/admin/create-client", json=dict(NEW)).get_json()["ok"] is True
    dup = _post(admin, "/admin/create-client", json=dict(NEW, username="PacificGrill", owner_email="b@pacific.test",
                                                         google_place_id="")).get_json()
    assert dup["ok"] is False and "already exists" in dup["error"]


def test_the_contract_is_never_sent_from_a_local_backend(admin, db_path, inline_jobs, monkeypatch):
    import scheduler
    import docusign_helper
    sent = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: sent.append(k) or {"envelope_id": "E1"})
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places", lambda pid, restaurant_id=None: None)
    body = _post(admin, "/admin/create-client", json=dict(NEW)).get_json()
    setup = _job(admin, "create-client", body["setup_job_id"])
    assert setup["docusign_skipped"] is True and "local" in setup["docusign_error"]
    row = get_restaurant(body["restaurant_id"])
    assert sent == [] and row.contract_status != "sent" and row.docusign_envelope_id is None


def test_on_railway_the_setup_job_sends_the_contract_and_fetches_the_menu(admin, db_path, inline_jobs, monkeypatch):
    import scheduler
    import docusign_helper
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(docusign_helper, "send_contract",
                        lambda **k: {"envelope_id": "ENV-%d" % k["module_count"]})
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places",
                        lambda pid, restaurant_id=None: "Menu from Places: fish tacos")
    body = _post(admin, "/admin/create-client", json=dict(NEW)).get_json()
    assert body["ok"] is True and body["setup_job_id"] and body["docusign_skipped"] is False
    setup = _job(admin, "create-client", body["setup_job_id"])
    assert setup == dict(setup, ok=True, envelope_id="ENV-2", docusign_skipped=False, menu_notes_fetched=True)
    row = get_restaurant(body["restaurant_id"])
    assert (row.contract_status, row.docusign_envelope_id) == ("sent", "ENV-2")
    assert row.menu_notes == "Menu from Places: fish tacos" and row.reviews_live == 1


def test_the_setup_job_never_overwrites_notes_typed_meanwhile(db_path, monkeypatch):
    rid = _rid(db_path, menu_notes="Typed by Will")
    monkeypatch.setattr(competitor, "fetch_menu_notes_from_places", lambda pid, restaurant_id=None: "From Places")
    job = "job-typed"
    admin_routes._ops.start_async_job(job, "client_setup", rid)
    admin_routes._run_client_setup(job, rid, {"google_place_id": "ChIJx", "fetch_menu": True, "module_names": [],
                                              "restaurant_name": "Client Grill", "owner_email": "c@x.test"})
    assert get_restaurant(rid).menu_notes == "Typed by Will"
    assert admin_routes._ops.read_async_job(job)["result"]["menu_notes_fetched"] is False


# ── #23: branding ───────────────────────────────────────────────────────────

def test_the_brand_get_returns_what_is_stored(admin, db_path):
    rid = _rid(db_path, brand_name="EJ's", brand_color="#c84b2f", brand_logo_url="https://cdn.example/logo.png")
    got = admin.get(f"/admin/api/brand/{rid}").get_json()
    assert got["ok"] is True and got["brand_name"] == "EJ's" and got["brand_color"] == "#c84b2f"
    assert got["version"] == restaurant_version(rid) and "profile" in got and "exclude_from_learning" in got


def test_blank_brand_fields_never_wipe_the_stored_ones(admin, db_path):
    rid = _rid(db_path, brand_name="EJ's", brand_color="#c84b2f", brand_logo_url="https://cdn.example/logo.png")
    r = _post(admin, f"/admin/api/brand/{rid}", json={"brand_name": "", "brand_color": "", "brand_logo_url": "",
                                                        "exclude_from_learning": 1})
    assert r.get_json()["ok"] is True
    row = get_restaurant(rid)
    assert (row.brand_name, row.brand_color, row.brand_logo_url) == ("EJ's", "#c84b2f", "https://cdn.example/logo.png")
    assert row.exclude_from_learning == 1
    cleared = _post(admin, f"/admin/api/brand/{rid}", json={"clear": ["brand_logo_url"]}).get_json()
    assert cleared["ok"] is True and cleared["brand_logo_url"] is None and cleared["brand_name"] == "EJ's"


@pytest.mark.parametrize("color,stored", [("#ABC", "#aabbcc"), ("c84b2f", "#c84b2f"), ("#C84B2F", "#c84b2f")])
def test_brand_colors_are_normalised(admin, db_path, color, stored):
    rid = _rid(db_path)
    assert _post(admin, f"/admin/api/brand/{rid}", json={"brand_color": color}).get_json()["ok"] is True
    assert get_restaurant(rid).brand_color == stored


@pytest.mark.parametrize("body", [{"brand_color": "red;}body{background:url(https://x)"}, {"brand_color": "#12345"},
                                  {"brand_logo_url": "javascript:alert(1)"}, {"brand_logo_url": "http://x/logo.png"},
                                  {"brand_logo_url": "https://x/a\" onerror=\"alert(1)"}])
def test_a_bad_color_or_logo_is_refused(admin, db_path, body):
    rid = _rid(db_path, brand_color="#111111")
    r = _post(admin, f"/admin/api/brand/{rid}", json=body)
    assert r.status_code == 400 and get_restaurant(rid).brand_color == "#111111"


def test_a_stale_brand_form_is_a_409(admin, db_path):
    rid = _rid(db_path)
    version = admin.get(f"/admin/api/brand/{rid}").get_json()["version"]
    update_restaurant(rid, {"brand_name": "Changed elsewhere"})
    r = _post(admin, f"/admin/api/brand/{rid}", json={"brand_name": "Mine", "expected_version": version})
    assert r.status_code == 409 and r.get_json()["current"]["brand_name"] == "Changed elsewhere"
    assert get_restaurant(rid).brand_name == "Changed elsewhere"


# ── #142: staff notes, ingredients, recipes ────────────────────────────────

def test_staff_note_routes_add_and_say_what_they_removed(admin, db_path):
    rid = _rid(db_path)
    first = _post(admin, f"/admin/staff-notes/{rid}", data={"employee_name": "Maria", "notes": "mornings only"})
    second = _post(admin, f"/admin/staff-notes/{rid}", data={"employee_name": "Maria", "notes": "no Sundays"})
    assert first.get_json()["appended"] is False and second.get_json()["appended"] is True
    assert second.get_json()["notes"] == "mornings only; no Sundays"
    gone = _post(admin, f"/admin/staff-notes/{second.get_json()['id']}/delete").get_json()
    assert gone["deleted"] == {"employee_name": "Maria", "notes": "mornings only; no Sundays", "restaurant_id": rid}
    assert _post(admin, f"/admin/staff-notes/{second.get_json()['id']}/delete").status_code == 404
    conn = models.get_conn(db_path)
    rows = conn.execute("SELECT event_type, restaurant_id, actor, before_json, after_json FROM admin_events "
                        "WHERE event_type LIKE 'staff_note.%' ORDER BY id").fetchall()
    conn.close()
    assert [r["event_type"] for r in rows] == ["staff_note.saved", "staff_note.saved", "staff_note.removed"]
    assert all(r["restaurant_id"] == rid and r["actor"] == "will" for r in rows)
    # Typed before/after (integration wave: the one audit call, record_admin_action).
    assert json.loads(rows[-1]["before_json"])["notes"] == "mornings only; no Sundays"
    assert json.loads(rows[-1]["after_json"]) == {"notes": None}


@pytest.mark.parametrize("field,value", [("par_level", "abc"), ("unit_cost", "NaN"), ("current_stock", -2),
                                         ("case_size", 0), ("unit_cost", "inf"), ("par_level", True)])
def test_a_bad_ingredient_number_is_refused_not_a_500(admin, db_path, field, value):
    rid = _rid(db_path)
    r = _post(admin, f"/admin/inventory/ingredients/{rid}", json={"name": "Salt", field: value})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_ingredient_names_lose_their_markup(admin, db_path):
    rid = _rid(db_path)
    r = _post(admin, f"/admin/inventory/ingredients/{rid}",
              json={"name": "<img src=x onerror=alert(1)>Salt", "unit": "<b>lb</b>", "category": "dry"})
    assert r.get_json()["ok"] is True
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT name, unit FROM ingredients WHERE id=?", (r.get_json()["id"],)).fetchone()
    conn.close()
    assert "<" not in row["name"] and ">" not in row["name"] and row["unit"] == "blb/b"


def test_a_bad_recipe_quantity_says_so(admin, db_path):
    import inventory_ledger
    rid = _rid(db_path)
    mid = inventory_ledger.create_menu_item(rid, "Pizza")
    ing = inventory_ledger.create_ingredient(rid, "Cheese")
    for qty in ("abc", 0, -1, "NaN"):
        r = _post(admin, f"/admin/inventory/recipes/{mid}", json={"ingredient_id": ing, "qty_per_unit": qty})
        assert r.status_code == 400 and "quantity" in r.get_json()["error"], qty
    bad_id = _post(admin, f"/admin/inventory/recipes/{mid}", json={"ingredient_id": "x", "qty_per_unit": 1})
    assert bad_id.status_code == 400
    assert _post(admin, f"/admin/inventory/recipes/{mid}",
                 json={"ingredient_id": ing, "qty_per_unit": "0.25"}).get_json()["ok"] is True


def test_a_bad_resync_date_is_refused(admin, db_path):
    rid = _rid(db_path)
    r = _post(admin, f"/admin/inventory/resync-depletion/{rid}", json={"business_date": "yesterday"})
    assert r.status_code == 400


# ── #73: alert contacts ────────────────────────────────────────────────────

def test_alert_contacts_are_normalised_capped_and_scoped(admin, db_path):
    from notify import get_alert_contacts
    rid, other = _rid(db_path), _rid(db_path, "Other Place")
    a = _post(admin, f"/admin/api/alert-contacts/{rid}", json={"name": "Sam", "phone": "(312) 555-0100"}).get_json()
    assert a["ok"] is True and a["phone"] == "+13125550100"
    dup = _post(admin, f"/admin/api/alert-contacts/{rid}", json={"name": "Sam", "phone": "312.555.0100"})
    assert dup.status_code == 400
    assert _post(admin, f"/admin/api/alert-contacts/{rid}", json={"phone": "3125550101"}).get_json()["ok"] is True
    third = _post(admin, f"/admin/api/alert-contacts/{rid}", json={"phone": "3125550102"})
    assert third.status_code == 400 and "limited" in third.get_json()["error"]
    contacts = get_alert_contacts(rid)
    assert len(contacts) == 2 and not any(c["sms_consent"] for c in contacts)
    wrong = _post(admin, f"/admin/api/alert-contacts/{other}/{a['id']}/delete")
    assert wrong.status_code == 404 and len(get_alert_contacts(rid)) == 2
    assert _post(admin, f"/admin/api/alert-contacts/{rid}/{a['id']}/delete").get_json()["ok"] is True
    assert [c["phone"] for c in get_alert_contacts(rid)] == ["+13125550101"]
