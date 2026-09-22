"""The dashboard shell (`GET /`) renders for a brand-new account.

hosted_dashboard boots the real app (schema, seed thread, blueprints) at
import, so this runs it in a subprocess against a scratch volume with an
empty reviews.db pre-created (models.adopt_legacy_db would otherwise copy
the developer's ./reviews.db into the scratch volume). A mis-bracketed
`next(..., 0)` in index() made `/` a 500 for every restaurant with no
responded review — both demo accounts as seeded — from Sep 19 until the
Sep 21 re-audit caught it; nothing rendered the home page for a fresh
account until this test.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r'''
import os, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()   # pre-create: no legacy adoption
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Fresh Co", owner_email="fresh@x.test", module_reviews=1))
uid = auth.create_user(rid, "fresh", "fresh@x.test", "correct-horse-battery", is_admin=False)
c = h.app.test_client()
import re
page = c.get("/login").get_data(as_text=True)          # sets the csrf_token cookie
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "fresh", "password": "correct-horse-battery", "csrf_token": token}, follow_redirects=False)
assert r.status_code in (302, 303), (r.status_code, r.get_data(as_text=True)[:300])
r = c.get("/")
print("HOME", r.status_code)
assert r.status_code == 200, r.get_data(as_text=True)[-800:]
assert b'id="review-diagnosis"' in r.data
'''


def test_the_dashboard_shell_renders_for_a_restaurant_with_no_responded_review():
    vol = tempfile.mkdtemp(prefix="cavnar-home-")
    out = subprocess.run([sys.executable, "-c", SCRIPT, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    assert "HOME 200" in out.stdout
