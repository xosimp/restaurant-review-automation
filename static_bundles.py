"""
static_bundles.py — the dashboard's static script and style blocks served
as content-hashed files (scheduler audit 10/10/26, SCALE-28).

templates/dashboard.html is 3.2 MB: about 2.2 MB of inline JavaScript and
0.6 MB of inline CSS, sent (and gzipped, ~46 ms of CPU) on every page load
of every owner, none of it cacheable — the page is per-request HTML. The
blocks that hold no Jinja are the same bytes on every load, so the template
loader lifts each such block of BUNDLE_MIN_BYTES or more out of the source
Jinja compiles (BundlingLoader) and puts a reference to its content hash in
its place: `<script src="/b/<hash>.js"></script>` where the script stood,
`<link rel="stylesheet" href="/b/<hash>.css">` where the style stood. Order
and globals are unchanged — a classic external script without async or
defer runs where the inline one did, and a stylesheet link cascades where
the style did. The file in the repository is unchanged (it stays the one
source, and every test that reads it reads what it always read).

The hash is in the URL, so the bundle is served with a year's immutable
max-age: a changed block is a new URL (CLAUDE.md: no far-future max-age on
anything that is not hashed or versioned — these are). Gzipped once, in
memory. A worker that has not yet compiled the template compiles it on the
first request for a bundle, so every worker answers every hash its template
produces; a hash from a template no longer deployed is a 404, which only a
page loaded before the deploy can ask for (and a reload fixes).
"""
import gzip
import hashlib
import re
import threading

import jinja2

# Templates whose static blocks are bundled, and the smallest block worth a
# request of its own.
BUNDLED_TEMPLATES = ("dashboard.html",)
BUNDLE_MIN_BYTES = 8000
BUNDLE_PATH = "/b/"
BUNDLE_MAX_AGE = 31536000

_bundles = {}           # "<hash>.js" / "<hash>.css" -> (text, gzipped bytes, mimetype)
_lock = threading.Lock()

_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
_STYLE = re.compile(r"<style(?P<attrs>[^>]*)>(?P<body>.*?)</style>", re.S | re.I)
_JINJA = re.compile(r"\{\{|\{%|\{#")
_JS_TYPES = ("", "text/javascript", "application/javascript")


def _register(body, ext, mimetype):
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]
    name = f"{digest}.{ext}"
    with _lock:
        if name not in _bundles:
            raw = body.encode("utf-8")
            _bundles[name] = (raw, gzip.compress(raw, compresslevel=9), mimetype)
    return name


def _attr(attrs, name):
    m = re.search(r"\b" + name + r"\s*=\s*([\"'])(.*?)\1", attrs or "", re.I)
    return m.group(2).strip().lower() if m else None


def bundle_source(source, min_bytes=None):
    """`source` with every static script and style block of `min_bytes` or
    more replaced by a reference to its bundle. A block is static when it
    holds no Jinja; a script is lifted only when it is plain JavaScript with
    no src, nonce, async, defer or module type of its own."""
    floor = BUNDLE_MIN_BYTES if min_bytes is None else int(min_bytes)

    def script(m):
        attrs, body = m.group("attrs") or "", m.group("body") or ""
        if len(body) < floor or _JINJA.search(body):
            return m.group(0)
        if (_attr(attrs, "type") or "") not in _JS_TYPES or re.search(r"\b(src|nonce|async|defer)\b", attrs, re.I):
            return m.group(0)
        name = _register(body, "js", "application/javascript")
        # Its own attributes ride along (an id a test or a reader names).
        keep = (" " + attrs.strip()) if attrs.strip() else ""
        return f'<script src="{BUNDLE_PATH}{name}"{keep}></script>'

    def style(m):
        attrs, body = m.group("attrs") or "", m.group("body") or ""
        if len(body) < floor or _JINJA.search(body) or re.search(r"\b(nonce|media|scoped)\b", attrs, re.I):
            return m.group(0)
        name = _register(body, "css", "text/css")
        ident = _attr(attrs, "id")
        return f'<link rel="stylesheet" href="{BUNDLE_PATH}{name}"' + (f' id="{ident}"' if ident else "") + ">"

    return _STYLE.sub(style, _SCRIPT.sub(script, source))


class BundlingLoader(jinja2.BaseLoader):
    """A loader that hands Jinja the BUNDLED_TEMPLATES with their static
    blocks lifted out (bundle_source), every other template as `inner`
    gives it."""

    def __init__(self, inner, names=BUNDLED_TEMPLATES):
        self.inner = inner
        self.names = tuple(names)

    def get_source(self, environment, template):
        source, filename, uptodate = self.inner.get_source(environment, template)
        if template in self.names:
            source = bundle_source(source)
        return source, filename, uptodate

    def list_templates(self):
        return self.inner.list_templates()


def install(app):
    """Bundle the dashboard's static blocks on `app` and serve them."""
    app.jinja_loader = BundlingLoader(app.jinja_loader)
    app.add_url_rule(BUNDLE_PATH + "<name>", "static_bundle", lambda name: serve(app, name))


def serve(app, name):
    """One bundle, immutable for a year, gzipped when the client takes it."""
    from flask import Response, abort, request
    if not re.fullmatch(r"[0-9a-f]{20}\.(js|css)", name or ""):
        abort(404)
    with _lock:
        hit = _bundles.get(name)
    if hit is None:
        # This worker has not compiled the template yet: compiling it
        # registers every bundle it references.
        for t in BUNDLED_TEMPLATES:
            try:
                app.jinja_env.get_template(t)
            except Exception:
                pass
        with _lock:
            hit = _bundles.get(name)
    if hit is None:
        abort(404)
    raw, packed, mimetype = hit
    gz = "gzip" in (request.headers.get("Accept-Encoding") or "").lower()
    resp = Response(packed if gz else raw, mimetype=mimetype)
    if gz:
        resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(packed if gz else raw))
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["Cache-Control"] = f"public, max-age={BUNDLE_MAX_AGE}, immutable"
    return resp


def stats() -> dict:
    with _lock:
        return {"bundles": len(_bundles), "bytes": sum(len(v[0]) for v in _bundles.values()),
                "gzipped": sum(len(v[1]) for v in _bundles.values())}
