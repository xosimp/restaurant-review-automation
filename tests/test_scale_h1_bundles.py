"""Scheduler audit 10/10/26, workstream H1 — SCALE-28: the dashboard's static
script and style blocks are served as content-hashed, immutable files
instead of 1.2 MB of inline text on every page load (static_bundles)."""
import os
import re
import subprocess
import sys
import tempfile

import jinja2
from flask import Flask

import static_bundles

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dashboard():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_dashboards_static_blocks_leave_the_page_and_the_dynamic_ones_stay():
    src = _dashboard()
    out = static_bundles.bundle_source(src)
    assert len(out) < 0.7 * len(src), (len(src), len(out))
    # Every block that is still inline is either small or holds Jinja.
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", out, re.S):
        body = m.group(2)
        assert len(body) < static_bundles.BUNDLE_MIN_BYTES or re.search(r"\{\{|\{%|\{#", body) \
            or "type=" in m.group(1), m.group(1)
    # In place: as many script tags as before, in the same order of ids.
    ids = lambda s: re.findall(r'<script[^>]*\bid="([^"]+)"', s)       # noqa: E731
    assert len(re.findall(r"<script\b", out)) == len(re.findall(r"<script\b", src))
    assert ids(out) == ids(src)
    # Still a template Jinja reads.
    jinja2.Environment().parse(out)


def test_a_bundle_is_served_immutable_and_gzipped_and_an_unknown_one_is_not():
    app = Flask(__name__)
    big = "var x = 1;\n" * 2000
    app.jinja_loader = jinja2.DictLoader({"dashboard.html": "<p>{{ who }}</p><script>" + big + "</script>"
                                                            "<style>" + ("a{b:c}" * 3000) + "</style>"})
    static_bundles.install(app)
    with app.test_request_context():
        html = app.jinja_env.get_template("dashboard.html").render(who="Ana")
    refs = re.findall(r'/b/([0-9a-f]{20}\.(?:js|css))', html)
    assert len(refs) == 2 and "<p>Ana</p>" in html and big not in html
    c = app.test_client()
    r = c.get("/b/" + refs[0], headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["Content-Encoding"] == "gzip"
    assert "immutable" in r.headers["Cache-Control"] and "max-age=31536000" in r.headers["Cache-Control"]
    import gzip
    assert gzip.decompress(r.data).decode() == big
    assert c.get("/b/" + refs[0]).data.decode() == big            # a client that takes no gzip
    assert c.get("/b/" + "0" * 20 + ".js").status_code == 404
    assert c.get("/b/../secret.js").status_code == 404


SCRIPT = r'''
import os, re, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import dotenv; dotenv.load_dotenv = lambda *a, **k: False  # never production's .env (audit #25)
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Bundle Co", owner_email="b@x.test", module_reviews=1, module_labor=1,
                                          module_inventory=1, module_marketing=1))
auth.create_user(rid, "bund", "b@x.test", "correct-horse-battery", is_admin=False)
c = h.app.test_client()
page = c.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "bund", "password": "correct-horse-battery", "csrf_token": token})
assert r.status_code in (302, 303), r.status_code
r = c.get("/", headers={"Accept-Encoding": "gzip"})
assert r.status_code == 200
import gzip
html = gzip.decompress(r.data).decode() if r.headers.get("Content-Encoding") == "gzip" else r.get_data(as_text=True)
refs = sorted(set(re.findall(r'/b/[0-9a-f]{20}\.(?:js|css)', html)))
ok = [c.get(u).status_code for u in refs]
print("PAGE", len(html), "REFS", len(refs), "OK", all(s == 200 for s in ok))
'''


def test_the_real_dashboard_references_bundles_every_worker_serves():
    vol = tempfile.mkdtemp(prefix="cavnar-bundles-")
    out = subprocess.run([sys.executable, "-c", SCRIPT, vol], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    line = next(l for l in out.stdout.splitlines() if l.startswith("PAGE "))
    _, size, _, refs, _, ok = line.split()
    assert int(refs) >= 20 and ok == "True"
    assert int(size) < 0.75 * len(_dashboard())
