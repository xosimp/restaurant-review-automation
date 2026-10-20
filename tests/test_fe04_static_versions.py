"""Versioned /static references (scale audit 10/19/26 FE-04): every page load
revalidated each /static asset against the four-thread web process (~29
conditional requests per dashboard load), because the files were referenced
by bare path and a far-future max-age would strand a fix in browser caches.
References now carry the file's content hash, and only a request carrying
the current hash is answered immutable."""
import os
import re

import jinja2
from flask import Flask

import static_bundles

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app(tmp_path, templates):
    static = tmp_path / "static"
    (static / "fonts").mkdir(parents=True)
    (static / "fonts" / "a.woff2").write_bytes(b"font-bytes")
    (static / "fonts" / "faces.css").write_text("@font-face{src:url('/static/fonts/a.woff2') format('woff2')}")
    (static / "app.js").write_text("var a = 1;")
    app = Flask(__name__, static_folder=str(static))
    app.jinja_loader = jinja2.DictLoader(templates)
    static_bundles.install(app)
    return app, static


def test_a_template_reference_carries_the_files_hash(tmp_path):
    app, static = _app(tmp_path, {"page.html": '<script src="/static/app.js"></script>'
                                              '<img src="/static/missing.png">'
                                              "<script>var u = '/static/img/lit-' + t + '-v1.png';</script>"})
    with app.test_request_context():
        html = app.jinja_env.get_template("page.html").render()
    v = static_bundles.asset_version("app.js", str(static))
    assert f'src="/static/app.js?v={v}"' in html
    assert 'src="/static/missing.png"' in html                       # no file, left alone
    assert "'/static/img/lit-' + t + '-v1.png'" in html              # built at run time, left alone


def test_only_the_current_hash_is_immutable(tmp_path):
    app, static = _app(tmp_path, {})
    c = app.test_client()
    v = static_bundles.asset_version("app.js", str(static))
    hit = c.get(f"/static/app.js?v={v}")
    assert hit.status_code == 200 and "immutable" in hit.headers["Cache-Control"]
    assert "max-age=31536000" in hit.headers["Cache-Control"]
    for url in ("/static/app.js", "/static/app.js?v=stale0000000"):
        r = c.get(url)
        assert r.status_code == 200 and "immutable" not in (r.headers.get("Cache-Control") or "")
    # A changed file is a new hash.
    (static / "app.js").write_text("var a = 2;")
    os.utime(static / "app.js", ns=(1, 1))
    assert static_bundles.asset_version("app.js", str(static)) != v


def test_a_stylesheets_fonts_are_versioned_inside_it(tmp_path):
    app, static = _app(tmp_path, {})
    c = app.test_client()
    css = c.get("/static/fonts/faces.css").get_data(as_text=True)
    fv = static_bundles.asset_version("fonts/a.woff2", str(static))
    assert f"url('/static/fonts/a.woff2?v={fv}')" in css
    cv = static_bundles.asset_version("fonts/faces.css", str(static))
    r = c.get(f"/static/fonts/faces.css?v={cv}")
    assert "immutable" in r.headers["Cache-Control"] and r.mimetype == "text/css"
    # Conditional: the browser's copy revalidates with a bodiless 304.
    etag = r.headers["ETag"]
    assert c.get("/static/fonts/faces.css", headers={"If-None-Match": etag}).status_code == 304
    assert c.get("/static/../secret").status_code == 404


def test_the_real_templates_reference_versioned_assets():
    app = Flask(__name__, static_folder=os.path.join(ROOT, "static"),
                template_folder=os.path.join(ROOT, "templates"))
    static_bundles.install(app)
    src, _f, _u = app.jinja_loader.get_source(app.jinja_env, "login.html")
    refs = re.findall(r'"/static/([^"]+)"', src)
    assert refs and all("?v=" in r for r in refs), refs
