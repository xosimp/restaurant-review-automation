"""Edge cases for marketing photos, drafts and tracked links
(marketing_media.py, marketing_drafts.py, marketing_links.py and the web and
mobile routes in front of them).

What these protect:

  * a tiny file with a huge canvas is refused before it is decoded — one
    such upload costs ~580 MB in the single web process (MOD-MKT-13);
  * an upload over the real app's body cap gets a JSON answer the web and iOS
    clients can show, the advertised per-file limit is reachable, and a
    format the upload gate accepts is one the server can decode (MOD-MKT-14);
  * a photo still in use cannot be deleted into a 500, and a delete of
    nothing does not report success (MOD-MKT-18);
  * a draft only ever carries its own restaurant's photo (MOD-MKT-1), and an
    unapproved draft cannot be published by the teammate who wrote it
    (MOD-MKT-17);
  * a tracked link counts people, not HEAD requests, link-preview bots or a
    flood (MOD-MKT-18), and forwards odd-but-valid targets correctly.

The 413 tests boot the real app (hosted_dashboard) in a subprocess against a
scratch volume, the way tests/test_home_page_renders.py does: the body cap
and the error handlers are app-level, so a blueprint-only test app would be
testing a configuration production does not have.
"""
import ast
import base64
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta

import pytest
from flask import Flask

import auth
import client_api
import marketing_drafts
import marketing_links
import marketing_media
import marketing_publish as mp
import mobile_api
import models
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mkt_media_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    """A copy of a module-built template (init_db + ensure_columns +
    init_auth) instead of conftest's per-test migration run."""
    import shutil
    path = str(tmp_path / "test_reviews.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_template_db + suffix):
            shutil.copy(_template_db + suffix, path + suffix)
    return path


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, marketing_media, marketing_drafts, marketing_links, mp):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


def _restaurant(db_path, name="Media Co", connect=True):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@media.test",
                                       module_marketing=1, timezone="America/Chicago"), db_path=db_path)
    if connect:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET ig_token='igt', ig_user_id='igu', fb_page_token='fbt', "
                     "fb_page_id='fbp' WHERE id=?", (rid,))
        conn.commit()
        conn.close()
    return rid


CSRF = "csrf-test-token"


class Session:
    """A real session row, carried as the web cookie or the mobile Bearer."""

    def __init__(self, app, db_path, rid, role="client", username="owner"):
        self.client = app.test_client()
        uid = auth.create_user(rid, username, f"{username}{rid}@media.test", "correct-horse-battery", db_path=db_path)
        auth.set_user_role(uid, role, db_path=db_path)
        self.uid = uid
        self.token = auth.create_session(uid, restaurant_id=rid, db_path=db_path)
        self.client.set_cookie("session_token", self.token)
        self.client.set_cookie("csrf_js", CSRF)

    def _headers(self, path, kw):
        headers = dict(kw.pop("headers", {}) or {})
        if path.startswith("/mobile/api/"):
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["X-CSRF"] = CSRF
        return headers

    def post(self, path, **kw):
        return self.client.post(path, headers=self._headers(path, kw), **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers=self._headers(path, kw), **kw)


def _png(size=(40, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (190, 70, 40)).save(buf, format="PNG")
    return buf.getvalue()


# ── Media #5 decompression bomb (MOD-MKT-13) ──────────────────────────────

@pytest.fixture(scope="module")
def bomb_png():
    """13000x13000 1-bit PNG: ~20 KB on the wire, 169M pixels once decoded
    (under Pillow's own DecompressionBombError threshold of ~179M)."""
    import warnings
    from PIL import Image
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        Image.new("1", (13000, 13000), 0).save(buf, "PNG", optimize=True)
    return buf.getvalue()


class _DecodedTheBomb(BaseException):
    """Raised by the load() guard below. A BaseException, so store_image's
    own `except Exception` cannot swallow it into a MediaError and let the
    test pass on the wrong path."""


@pytest.mark.xfail(strict=True, reason="MOD-MKT-13: store_image decodes the full canvas (exif_transpose/convert) before any pixel-count check")
def test_a_tiny_file_with_a_huge_canvas_is_refused_before_it_is_decoded(db_path, monkeypatch, bomb_png):
    """A6 Media #5 / MOD-MKT-13: the check has to be on the image size read
    from the header, not on the compressed byte count. The guard stops the
    real decode (so the test itself does not spend ~580 MB) and records it."""
    from PIL import ImageFile
    assert len(bomb_png) < 100_000
    real_load = ImageFile.ImageFile.load

    def guarded_load(self):
        if self.size[0] * self.size[1] > 50_000_000:
            raise _DecodedTheBomb(f"decoded a {self.size[0]}x{self.size[1]} canvas")
        return real_load(self)
    monkeypatch.setattr(ImageFile.ImageFile, "load", guarded_load)

    rid = _restaurant(db_path)
    decoded = None
    refused = None
    try:
        marketing_media.store_image(rid, bomb_png, "image/png", db_path=db_path)
    except marketing_media.MediaError as e:
        refused = e
    except _DecodedTheBomb as e:
        decoded = e
    assert decoded is None, f"store_image started decoding the bomb: {decoded}"
    assert refused is not None


def test_an_ordinary_large_photo_is_still_accepted_and_downscaled(db_path):
    """A6 Media #5 control: a real 12-megapixel phone photo is not a bomb."""
    rid = _restaurant(db_path)
    stored = marketing_media.store_image(rid, _png(size=(4032, 3024)), "image/png", db_path=db_path)
    assert max(stored["width"], stored["height"]) == marketing_media.MAX_EDGE


# ── Media #6 body over the app cap (MOD-MKT-14) ───────────────────────────

_BOOT_SCRIPT = r'''
import io, json, os, re, sqlite3, sys, base64
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models
from models import Restaurant
from PIL import Image
rid = models.create_restaurant(Restaurant(name="Upload Co", owner_email="up@x.test", module_marketing=1))
auth.create_user(rid, "upload", "up@x.test", "correct-horse-battery")
out = {"max_content_length": h.app.config.get("MAX_CONTENT_LENGTH")}

def shape(r):
    body = r.get_json(silent=True)
    return {"status": r.status_code, "mimetype": r.mimetype, "json": body}

m = h.app.test_client()
token = m.post("/mobile/api/login", json={"username": "upload", "password": "correct-horse-battery"}).get_json()["token"]
bearer = {"Authorization": "Bearer " + token}
small = io.BytesIO(); Image.new("RGB", (40, 30), (200, 80, 40)).save(small, "PNG")
out["mobile_small"] = shape(m.post("/mobile/api/marketing/media", headers=bearer,
                                   json={"image_base64": base64.b64encode(small.getvalue()).decode()}))
out["mobile_big"] = shape(m.post("/mobile/api/marketing/media", headers=bearer,
                                 json={"image_base64": base64.b64encode(os.urandom(4_600_000)).decode()}))

w = h.app.test_client()
page = w.get("/login").get_data(as_text=True)
form_token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
w.post("/login", data={"username": "upload", "password": "correct-horse-battery", "csrf_token": form_token})
w.get("/api/instagram-status")
csrf = w.get_cookie("csrf_js").value
out["web_big"] = shape(w.post("/api/marketing/media", headers={"X-CSRF": csrf}, content_type="multipart/form-data",
                              data={"file": (io.BytesIO(os.urandom(6 * 1024 * 1024)), "big.jpg", "image/jpeg")}))
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def real_app_uploads():
    vol = tempfile.mkdtemp(prefix="cavnar-mkt-413-")
    proc = subprocess.run([sys.executable, "-c", _BOOT_SCRIPT, vol], cwd=ROOT,
                          capture_output=True, text=True, timeout=180)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stdout[-1500:] + "\n" + proc.stderr[-2500:]
    return json.loads(lines[-1][len("RESULT "):])


def test_the_real_app_accepts_an_ordinary_upload_from_the_app(real_app_uploads):
    """A6 Media #6 control: the harness logs in and uploads for real."""
    small = real_app_uploads["mobile_small"]
    assert small["status"] == 200 and small["json"]["ok"] is True


@pytest.mark.parametrize("which", ["mobile_big", "web_big"])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-14: hosted_dashboard registers no 413 handler, so a body over MAX_CONTENT_LENGTH gets Flask's HTML page and the clients show 'Upload failed'")
def test_an_upload_over_the_body_cap_gets_a_json_refusal_the_client_can_show(real_app_uploads, which):
    """A6 Media #6 / MOD-MKT-14: the web JS does r.json() and iOS decodes
    JSON; an HTML 413 dead-ends both with no reason."""
    r = real_app_uploads[which]
    assert r["status"] == 413
    assert r["json"] is not None, f"{which} got {r['mimetype']}, not JSON"
    assert r["json"]["ok"] is False and r["json"].get("error")


def _source_max_content_length():
    """app.config['MAX_CONTENT_LENGTH'] as hosted_dashboard.py sets it, read
    from source so this test does not boot the app."""
    tree = ast.parse(open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].slice, ast.Constant)
                and node.targets[0].slice.value == "MAX_CONTENT_LENGTH"):
            return eval(compile(ast.Expression(node.value), "<cap>", "eval"), {"__builtins__": {}})
    raise AssertionError("MAX_CONTENT_LENGTH assignment not found in hosted_dashboard.py")


@pytest.mark.xfail(strict=True, reason="MOD-MKT-14: MAX_UPLOAD_BYTES (12 MB, and its 'pick one under 12MB' message) is above the app's 5 MB body cap, so it can never fire")
def test_the_per_photo_limit_the_owner_is_told_is_one_the_app_can_actually_receive(real_app_uploads):
    """A6 Media #6 / MOD-MKT-14. The mobile path base64-encodes (+33%), so
    the stated limit has to fit under the cap after encoding."""
    cap = real_app_uploads["max_content_length"]
    assert cap == _source_max_content_length()
    assert marketing_media.MAX_UPLOAD_BYTES * 4 / 3 <= cap


# ── Media #7 HEIC (MOD-MKT-14) ────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-14: image/heic and image/heif pass ALLOWED_MIME but no HEIF decoder is registered with Pillow (pillow-heif is not installed)")
def test_every_format_the_upload_gate_accepts_is_one_the_server_can_decode():
    """A6 Media #7 / MOD-MKT-14: a HEIC from a desktop file picker passes the
    MIME gate and then fails "That file didn't open as a photo"."""
    from PIL import Image
    Image.init()
    decodable = set(Image.registered_extensions())
    wanted = {"image/heic": ".heic", "image/heif": ".heif", "image/webp": ".webp",
              "image/png": ".png", "image/jpeg": ".jpg"}
    missing = [mime for mime in marketing_media.ALLOWED_MIME if wanted.get(mime) not in decodable]
    assert missing == []


# ── Media #8 base64 bodies ────────────────────────────────────────────────

def test_a_data_url_upload_from_the_app_is_stored(app, db_path):
    """A6 Media #8: the iOS picker can send `data:image/png;base64,...`."""
    rid = _restaurant(db_path)
    s = Session(app, db_path, rid)
    body = "data:image/png;base64," + base64.b64encode(_png()).decode()
    resp = s.post("/mobile/api/marketing/media", json={"image_base64": body})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert marketing_media.list_media(rid, db_path=db_path)


@pytest.mark.parametrize("payload, needle", [
    ("!!!!", "No photo"),                                                   # decodes to nothing
    (base64.b64encode(b"just some text, not a photo").decode(), "didn't open"),
    ("data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4").decode(), "isn't a photo"),
])
def test_a_bad_base64_body_is_a_clear_400_not_a_crash(app, db_path, payload, needle):
    """A6 Media #8: invalid base64, base64 of a non-image, and a data: URL
    declaring a non-image type each get a JSON 400 the app can show."""
    rid = _restaurant(db_path)
    s = Session(app, db_path, rid)
    resp = s.post("/mobile/api/marketing/media", json={"image_base64": payload})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False and needle in body["error"]
    assert marketing_media.list_media(rid, db_path=db_path) == []


# ── Media #9 delete while referenced ──────────────────────────────────────

def _referenced_photo(db_path, rid):
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    when = (mp._local_now(rid) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    assert mp.schedule_post(rid, "instagram", "Photo post", when, media_id=mine["id"], db_path=db_path)["ok"]
    return mine


@pytest.mark.parametrize("path", ["/api/marketing/media/{id}", "/mobile/api/marketing/media/{id}"])
@pytest.mark.xfail(strict=True, reason="MOD-A6-media-9: the media_id foreign key blocks the delete, but delete_media lets the IntegrityError escape and the route 500s with no reason")
def test_deleting_a_photo_a_scheduled_post_needs_is_refused_with_a_reason(app, db_path, path):
    """A6 Media #9 (MOD-MKT-18 said the delete was allowed; on a database
    built by init_db the FK refuses it — the defect is the 500)."""
    rid = _restaurant(db_path)
    mine = _referenced_photo(db_path, rid)
    s = Session(app, db_path, rid)
    resp = s.delete(path.format(id=mine["id"]))
    assert resp.status_code in (400, 409), resp.status_code
    body = resp.get_json()
    assert body and body["ok"] is False and body.get("error")


def test_a_photo_a_scheduled_post_needs_is_still_there_after_a_delete_attempt(app, db_path):
    """A6 Media #9: whatever the route says, the photo the post needs at
    publish time survives."""
    rid = _restaurant(db_path)
    mine = _referenced_photo(db_path, rid)
    s = Session(app, db_path, rid)
    s.delete(f"/api/marketing/media/{mine['id']}")
    assert marketing_media.get_image(mine["token"], db_path=db_path) is not None


@pytest.mark.parametrize("path", ["/api/marketing/media/{id}", "/mobile/api/marketing/media/{id}"])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: DELETE on a photo id that is not yours (or does not exist) returns ok=True")
def test_deleting_someone_elses_photo_does_not_report_success(app, db_path, path):
    """MOD-MKT-18: the delete is correctly scoped, but says it worked."""
    mine = _restaurant(db_path, name="Mine Co")
    theirs = _restaurant(db_path, name="Theirs Co")
    their_photo = marketing_media.store_image(theirs, _png(), "image/png", db_path=db_path)
    s = Session(app, db_path, mine)
    resp = s.delete(path.format(id=their_photo["id"]))
    assert marketing_media.get_image(their_photo["token"], db_path=db_path) is not None
    assert resp.get_json()["ok"] is False


# ── Media #10 empty file ──────────────────────────────────────────────────

def test_an_empty_file_is_refused_with_something_actionable(db_path):
    """A6 Media #10."""
    rid = _restaurant(db_path)
    with pytest.raises(marketing_media.MediaError, match="empty"):
        marketing_media.store_image(rid, b"", "image/png", db_path=db_path)


def test_an_empty_upload_is_a_json_400_on_web_and_mobile(app, db_path):
    """A6 Media #10: a zero-byte multipart file (web) and an empty base64
    body (iOS)."""
    rid = _restaurant(db_path)
    s = Session(app, db_path, rid)
    web = s.post("/api/marketing/media", content_type="multipart/form-data",
                 data={"file": (io.BytesIO(b""), "empty.jpg", "image/jpeg")})
    assert web.status_code == 400 and web.get_json()["ok"] is False
    mobile = s.post("/mobile/api/marketing/media", json={"image_base64": ""})
    assert mobile.status_code == 400 and "No photo" in mobile.get_json()["error"]
    assert marketing_media.list_media(rid, db_path=db_path) == []


# ── Drafts #5 foreign photo (MOD-MKT-1) ───────────────────────────────────

def test_a_draft_cannot_be_saved_with_another_restaurants_photo(db_path):
    """A6 Drafts #5 / MOD-MKT-1."""
    a = _restaurant(db_path, name="Alpha Co")
    b = _restaurant(db_path, name="Bravo Co")
    theirs = marketing_media.store_image(b, _png(), "image/png", db_path=db_path)
    assert not marketing_drafts.save_draft(a, "Our copy", media_id=theirs["id"], db_path=db_path)["ok"]


def test_the_drafts_listing_never_hands_back_another_restaurants_photo_token(db_path):
    """A6 Drafts #5 / MOD-MKT-1: probe p02_draft_idor reproduced the leak."""
    a = _restaurant(db_path, name="Alpha Co")
    b = _restaurant(db_path, name="Bravo Co")
    theirs = marketing_media.store_image(b, _png(), "image/png", db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO marketing_drafts (restaurant_id, body, media_id) VALUES (?,?,?)", (a, "Copy", theirs["id"]))
    conn.commit()
    conn.close()
    drafts = marketing_drafts.list_drafts(a, db_path=db_path)
    assert drafts, "setup: A's draft should be listed"
    assert all(d.get("media_token") != theirs["token"] for d in drafts)


def test_a_draft_with_its_own_photo_lists_that_photos_token(db_path):
    """A6 Drafts #5 control."""
    rid = _restaurant(db_path)
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    assert marketing_drafts.save_draft(rid, "Copy", media_id=mine["id"], db_path=db_path)["ok"]
    assert marketing_drafts.list_drafts(rid, db_path=db_path)[0]["media_token"] == mine["token"]


# ── Drafts #6 length ──────────────────────────────────────────────────────

def test_a_draft_is_kept_up_to_the_limit_and_refused_one_character_past_it(app, db_path):
    """A6 Drafts #6: MAX_BODY, on the module and through the route."""
    rid = _restaurant(db_path)
    limit = marketing_drafts.MAX_BODY
    assert marketing_drafts.save_draft(rid, "x" * limit, db_path=db_path)["ok"]
    over = marketing_drafts.save_draft(rid, "x" * (limit + 1), db_path=db_path)
    assert not over["ok"] and "longer" in over["error"]
    assert not marketing_drafts.save_draft(rid, "   \n  ", db_path=db_path)["ok"]
    s = Session(app, db_path, rid)
    resp = s.post("/api/marketing/drafts", json={"body": "x" * (limit + 1)})
    assert resp.status_code == 400
    assert len(marketing_drafts.list_drafts(rid, db_path=db_path)) == 1


# ── Drafts #7 approval is not enforced at publish (MOD-MKT-17) ────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-17: approval is advisory — a teammate refused at /approve can still schedule the same copy")
def test_a_teammate_cannot_route_around_approval_by_scheduling_the_draft(app, db_path):
    """A6 Drafts #7 / MOD-MKT-17: write → refused approval → schedule it
    anyway. The last step must be refused too."""
    rid = _restaurant(db_path)
    s = Session(app, db_path, rid, role="member", username="teammate")
    saved = s.post("/api/marketing/drafts", json={"body": "Unapproved special"}).get_json()
    assert saved["ok"]
    refused = s.post(f"/api/marketing/drafts/{saved['id']}/approve")
    assert refused.status_code == 403
    when = (mp._local_now(rid) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    resp = s.post("/api/marketing/schedule", json={"platform": "facebook", "body": "Unapproved special",
                                                   "scheduled_for": when})
    assert resp.status_code == 403
    assert mp.list_scheduled(rid, db_path=db_path) == []


# ── Links #5 / #6 what counts as a tap (MOD-MKT-18) ───────────────────────

def _clicks(db_path, rid):
    return marketing_links.link_stats(rid, db_path=db_path)[0]["clicks"]


def _link(db_path, rid, url="https://example.com/menu"):
    made = marketing_links.create_link(rid, url, source="sms", campaign="wings", db_path=db_path)
    assert made["ok"]
    return made


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"


def test_a_guest_tapping_the_link_is_counted_and_forwarded(app, db_path):
    """A6 Links #5 control."""
    rid = _restaurant(db_path)
    made = _link(db_path, rid)
    resp = app.test_client().get(f"/g/{made['token']}", headers={"User-Agent": IPHONE})
    assert resp.status_code == 302
    assert _clicks(db_path, rid) == 1


@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: every HEAD on /g/<token> increments clicks")
def test_a_head_request_is_not_counted_as_a_tap(app, db_path):
    """A6 Links #5 / MOD-MKT-18: link checkers and some messaging apps HEAD
    a URL before anyone taps it."""
    rid = _restaurant(db_path)
    made = _link(db_path, rid)
    resp = app.test_client().head(f"/g/{made['token']}")
    assert resp.status_code in (200, 302)
    assert _clicks(db_path, rid) == 0


@pytest.mark.parametrize("agent", [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Twitterbot/1.0",
    "WhatsApp/2.23.20.0",
])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: link-preview fetchers are counted as guest taps (no bot/previewer filter)")
def test_a_link_preview_fetch_is_not_counted_as_a_tap(app, db_path, agent):
    """A6 Links #5 / MOD-MKT-18: taps rank campaigns in
    guest_marketing.diagnose; a preview is not a guest."""
    rid = _restaurant(db_path)
    made = _link(db_path, rid)
    app.test_client().get(f"/g/{made['token']}", headers={"User-Agent": agent})
    assert _clicks(db_path, rid) == 0


@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: /g/<token> has no rate limit or de-duplication, so anyone with the link can inflate a campaign's taps")
def test_a_flood_of_hits_from_one_address_does_not_inflate_the_tap_count(app, db_path):
    """A6 Links #6 / MOD-MKT-18: 100 hits from one IP in one burst."""
    rid = _restaurant(db_path)
    made = _link(db_path, rid)
    client = app.test_client()
    for _ in range(100):
        client.get(f"/g/{made['token']}", headers={"User-Agent": IPHONE},
                   environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert _clicks(db_path, rid) < 100


# ── Links #7 odd targets ──────────────────────────────────────────────────

def test_a_unicode_host_is_forwarded_as_punycode(app, db_path):
    """A6 Links #7: an IDN host must reach the browser as a valid Location."""
    rid = _restaurant(db_path)
    made = _link(db_path, rid, "https://café.example/menu")
    resp = app.test_client().get(f"/g/{made['token']}")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://xn--caf-dma.example/menu")


def test_a_very_long_target_is_forwarded_intact(app, db_path):
    """A6 Links #7: a 4,000-character path (a tracking-heavy ordering link)."""
    rid = _restaurant(db_path)
    path = "a" * 4000
    made = _link(db_path, rid, f"https://example.com/{path}")
    resp = app.test_client().get(f"/g/{made['token']}")
    assert resp.status_code == 302
    assert f"/{path}?" in resp.headers["Location"]


def test_a_target_with_a_line_break_cannot_inject_a_header(app, db_path):
    """A6 Links #7: CR/LF in a stored target never becomes a new header."""
    rid = _restaurant(db_path)
    made = marketing_links.create_link(rid, "https://example.com/\r\nSet-Cookie: pwned=1", db_path=db_path)
    if made["ok"]:
        resp = app.test_client().get(f"/g/{made['token']}")
        assert "Set-Cookie" not in resp.headers
        assert "\n" not in resp.headers.get("Location", "")


@pytest.mark.xfail(strict=True, reason="MOD-A6-links-7: a target whose host contains a space is accepted and redirected to a broken Location")
def test_a_target_with_a_space_in_the_host_is_refused(db_path):
    """A6 Links #7: "exa mple.com" is not a web address; a guest tapping the
    text gets a broken page with the restaurant's name on the link."""
    rid = _restaurant(db_path)
    assert not marketing_links.create_link(rid, "https://exa mple.com/menu", db_path=db_path)["ok"]
