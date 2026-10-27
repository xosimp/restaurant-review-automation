"""Every endpoint path the clients name resolves to a registered route
(audit #25 A04-12 / A06-07).

Only iOS WRITES were pinned (tests/test_ios_request_body_parity.py). A route
renamed or removed left a dashboard fetch or an iPhone read calling a 404,
and the screen showed its empty state, not an error. This reads every path
literal in the web templates and static scripts (`'/api/…'`, `'/admin/…'`,
`'/staff/…'`, `'/mobile/api/…'`) and in the iOS app's Swift sources
(`"/mobile/api/…"`, `"/staff/…"`, interpolations as one segment) and matches
each against the url_map of every blueprint the app registers
(`blueprints.url_map_app()`, the list hosted_dashboard registers from):

- an exact literal must match a rule segment for segment (a converter
  matches any one segment);
- a literal that is a prefix — it ends in `/`, carries a `?query`, or is
  followed by `+` (the rest is concatenated at run time) — must be the start
  of some rule;
- where the call site names its HTTP method (blind re-audit 10/27/26) — a
  `fetch(path, {method: 'POST'})` or a bare `fetch(path)` (GET), one of the
  dashboard's verified helpers (`jget`/`getJ` GET, `postJ`/`jpost`/… POST),
  a Swift `send(path, method: .post)` or a transport whose default it is
  (`send` GET, `upload`/`stream`/`sendWithHeaders` POST) — an exact path
  must reach a rule that takes that method: a route narrowed to POST while
  the phone still GETs it is a 405 the path check alone passed.

A dynamic `fetch(url)` with no literal cannot be checked statically; nothing
here pretends to. iOS test fixtures (CavnarAITests) name fake endpoints on
purpose and are not scanned.
"""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# literal -> why it names no route (it is not a request).
NOT_A_REQUEST = {
    "/staff/api": "APIClient.isStaffAPI compares a path with the staff tier's prefix; nothing is requested",
}

_WEB = re.compile(r"""(['"])((?:/api/|/mobile/api/|/staff/|/admin/)[^'"\s<>{}]*)\1(\s*\+)?""")
_INTERP = r"\\\((?:[^()]|\([^()]*\))*\)"
_SWIFT = re.compile(r'"((?:/mobile/api/|/staff/)(?:[^"\\\n]|' + _INTERP + r'|\\.)*)"(\s*\+)?')


class _Rule(list):
    """A rule's segments (None for a converter), carrying its methods."""
    methods = frozenset()


def _segments(rule, methods=()):
    out = _Rule(None if s.startswith("<") else s for s in rule.strip("/").split("/"))
    out.methods = frozenset(m.upper() for m in (methods or ()))
    return out


@pytest.fixture(scope="module")
def rules():
    import blueprints
    app = blueprints.url_map_app(template_folder=os.path.join(ROOT, "templates"))
    return [_segments(r.rule, r.methods) for r in app.url_map.iter_rules() if r.endpoint != "static"]


def resolves(literal, prefix, rules, method=None):
    """Whether `literal` names a rule (exactly) or starts one (`prefix`).
    "{*}" is an interpolated segment and matches any one segment. With
    `method`, an exact literal must reach a rule that takes it (a prefix is
    matched on its path alone: what follows it is unknown)."""
    trailing = literal.endswith("/")
    parts = [p for p in literal.strip("/").split("/")] if literal.strip("/") else []
    for rs in rules:
        if (len(parts) > len(rs)) if prefix else (len(parts) != len(rs)):
            continue
        for i, part in enumerate(parts):
            r = rs[i]
            if part == "{*}" or r is None:
                continue
            if prefix and not trailing and i == len(parts) - 1:
                if not r.startswith(part):
                    break
            elif part != r:
                break
        else:
            if method and not prefix and getattr(rs, "methods", None) and method.upper() not in rs.methods:
                continue
            return True
    return False


# ── the method a call site names ────────────────────────────────────────────

_CALL_BEFORE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(\s*$")
# Dashboard helpers whose every definition in templates/ fixes the method
# (read 10/27/26): getJ/jget fetch with no method; the rest POST.
_JS_GET_HELPERS = {"jget", "getJ"}
_JS_POST_HELPERS = {"postJ", "jpost", "asPost", "tsPost", "gxPost", "_cpPost", "suPost", "agreePost", "postJson"}
# APIClient's transports and the method each uses when the call names none.
_SWIFT_DEFAULT = {"send": "GET", "sendKeepingBody": "GET", "sendInsight": "GET", "sendWithBearer": "GET",
                  "sendUnauthenticated": "GET", "fetchFile": "GET", "upload": "POST", "stream": "POST",
                  "sendWithHeaders": "POST",
                  # The staff tier's transport (StaffSessionStore.authed, method: .get by default)
                  # and its GET-only wrapper (StaffRequestsViews.staffGet).
                  "authed": "GET", "staffGet": "GET"}


def call_args(src, open_paren, swift=False):
    """The top-level argument texts of the call whose "(" is at
    `open_paren`, string literals and comments skipped; None when the call
    does not close."""
    args, depth, start, i, n = [], 0, open_paren + 1, open_paren + 1, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'`" and not (swift and c in "'`"):
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    if swift and j + 1 < n and src[j + 1] == "(":
                        # an interpolation: skip to its closing bracket
                        d, k = 1, j + 2
                        while k < n and d:
                            d += {"(": 1, ")": -1}.get(src[k], 0)
                            k += 1
                        j = k
                        continue
                    j += 2
                    continue
                if src[j] == "\n" and c != "`":
                    break
                j += 1
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                if src[start:i].strip():
                    args.append(src[start:i].strip())
                return args
            depth -= 1
        elif c == "," and depth == 0:
            args.append(src[start:i].strip())
            start = i + 1
        i += 1
    return None


def web_method(src, start):
    """The HTTP method of the call whose first argument is the path literal
    at `start`, or None when the call site does not say."""
    m = _CALL_BEFORE.search(src[max(0, start - 80):start])
    if not m:
        return None
    name = m.group(1)
    args = call_args(src, start - (len(src[max(0, start - 80):start]) - m.end()) - 1)
    if args is None:
        return None
    if name in _JS_GET_HELPERS:
        return "GET"
    if name in _JS_POST_HELPERS:
        return "POST"
    if name == "jsend":
        return "POST" if len(args) <= 3 else None
    if name != "fetch":
        return None
    if len(args) < 2:
        return "GET"
    opts = args[1]
    if not opts.startswith("{"):
        return None
    mm = re.search(r"\bmethod\s*:\s*(['\"])(\w+)\1", opts)
    if mm:
        return mm.group(2).upper()
    return None if re.search(r"\bmethod\s*:", opts) else "GET"


def swift_method(src, start):
    """The same for a Swift call: `method:` named as a literal, else the
    transport's own default."""
    m = _CALL_BEFORE.search(src[max(0, start - 80):start])
    if not m:
        return None
    name = m.group(1)
    args = call_args(src, start - (len(src[max(0, start - 80):start]) - m.end()) - 1, swift=True)
    if args is None:
        return None
    for a in args[1:]:
        mm = re.match(r"method\s*:\s*(.+)$", a, re.S)
        if mm:
            val = re.sub(r"\.rawValue$", "", mm.group(1).strip())
            lit = re.match(r'^(?:HTTPMethod|APIClient\.HTTPMethod)?\.(get|post|put|patch|delete)$', val) \
                or re.match(r'^"(GET|POST|PUT|PATCH|DELETE)"$', val)
            return lit.group(1).upper() if lit else None
    return _SWIFT_DEFAULT.get(name)


def _web_literals(root=None):
    """(file, line, raw, literal, prefix, method) for every path literal in
    the templates and static scripts; `method` None where the call site
    does not say."""
    root = root or ROOT
    out = []
    files = sorted(glob.glob(os.path.join(root, "templates", "**", "*.html"), recursive=True)
                   + glob.glob(os.path.join(root, "static", "**", "*.js"), recursive=True))
    for f in files:
        src = open(f, encoding="utf-8", errors="replace").read()
        for m in _WEB.finditer(src):
            raw = m.group(2)
            prefix = bool(m.group(3)) or raw.endswith(("/", "=")) or "?" in raw
            out.append((os.path.relpath(f, root), src.count("\n", 0, m.start()) + 1, raw,
                        raw.split("?")[0].split("#")[0], prefix, web_method(src, m.start())))
    return out


def _swift_literals(root=None):
    """The same for the iOS app's Swift sources (every target but the tests)."""
    root = root or ROOT
    out = []
    files = sorted(f for f in glob.glob(os.path.join(root, "ios", "CavnarAI", "**", "*.swift"), recursive=True)
                   if "/CavnarAITests/" not in f)
    for f in files:
        src = open(f, encoding="utf-8", errors="replace").read()
        for m in _SWIFT.finditer(src):
            raw = m.group(1)
            lit = re.sub(_INTERP, "{*}", raw)
            prefix = bool(m.group(2)) or raw.endswith("/") or "?" in lit
            lit = "/".join("{*}" if "{*}" in s else s for s in lit.split("?")[0].split("/"))
            out.append((os.path.relpath(f, root), src.count("\n", 0, m.start()) + 1, raw, lit, prefix,
                        swift_method(src, m.start())))
    return out


def _unresolved(literals, rules):
    out = []
    for f, line, raw, lit, prefix, method in literals:
        if raw in NOT_A_REQUEST:
            continue
        if not resolves(lit, prefix, rules):
            out.append(f"{f}:{line}: {raw}")
        elif method and not resolves(lit, prefix, rules, method=method):
            out.append(f"{f}:{line}: {method} {raw} (the route does not take {method})")
    return out


def test_the_scans_find_the_clients_paths():
    """Guards the guard: a regex that matched nothing would pass below."""
    web, swift = _web_literals(), _swift_literals()
    assert len(web) >= 800, len(web)
    assert len(swift) >= 700, len(swift)
    assert any(raw == "/api/staff-brief" for _, _, raw, _, _, _ in web)
    assert any(lit == "/mobile/api/staff-docs/{*}/remove" for _, _, _, lit, _, _ in swift)
    # ...and the method is read at most call sites, both ways.
    web_m = [x[5] for x in web if x[5]]
    swift_m = [x[5] for x in swift if x[5]]
    assert len(web_m) >= 400 and {"GET", "POST"} <= set(web_m), (len(web_m), set(web_m))
    assert len(swift_m) >= 400 and {"GET", "POST", "DELETE"} <= set(swift_m), (len(swift_m), set(swift_m))


def test_the_method_is_read_from_the_call_site():
    js = ("fetch('/api/a');\nfetch('/api/b', {method: 'POST', body: JSON.stringify({x: '(,)'})});\n"
          "fetch('/api/c', {credentials: 'same-origin'});\nfetch('/api/d', opts);\n"
          "postJ('/api/e', {}, cb);\njsend('/api/f', {}, cb, m);\nvar u = '/api/g';\n")
    got = [web_method(js, m.start()) for m in _WEB.finditer(js)]
    assert got == ["GET", "POST", "GET", None, "POST", None, None], got
    sw = ('try await api.send("/mobile/api/a")\n'
          'try await api.send("/mobile/api/b/\\(id)", method: .post, body: B(t: "x, y"))\n'
          'try await APIClient.shared.send("/mobile/api/c", method: .delete)\n'
          'try await client.upload("/mobile/api/d", fileData: d, filename: "f", mimeType: "x")\n'
          'try await send("/mobile/api/e", method: verb)\nlet p = "/mobile/api/f"\n')
    got = [swift_method(sw, m.start()) for m in _SWIFT.finditer(sw)]
    assert got == ["GET", "POST", "DELETE", "POST", None, None], got


def test_the_matcher_refuses_a_method_the_route_does_not_take():
    only_get = [_segments("/api/thing/<int:id>", ("GET", "HEAD", "OPTIONS"))]
    assert resolves("/api/thing/{*}", False, only_get, method="GET")
    assert not resolves("/api/thing/{*}", False, only_get, method="POST")
    assert _unresolved([("x.html", 1, "/api/thing/7", "/api/thing/7", False, "POST")], only_get) == [
        "x.html:1: POST /api/thing/7 (the route does not take POST)"]
    # A prefix is matched on its path alone.
    assert resolves("/api/thing/", True, only_get, method="POST")


def test_the_matcher_refuses_a_path_no_route_serves(rules):
    assert resolves("/api/staff-brief", False, rules)
    assert resolves("/mobile/api/staff-docs/{*}/remove", False, rules)
    assert resolves("/api/staff-docs/", True, rules)
    assert not resolves("/api/definitely-not-a-route", False, rules)
    assert not resolves("/mobile/api/staff-docs/{*}/vanish", False, rules)
    assert not resolves("/api/definitely-not", True, rules)


def test_every_dashboard_fetch_reaches_a_route(rules):
    bad = _unresolved(_web_literals(), rules)
    assert not bad, "web paths no route serves:\n" + "\n".join(bad)


def test_every_ios_path_reaches_a_route(rules):
    bad = _unresolved(_swift_literals(), rules)
    assert not bad, "iOS paths no route serves:\n" + "\n".join(bad)


def test_every_not_a_request_entry_is_still_in_the_app():
    raws = {x[2] for x in _swift_literals() + _web_literals()}
    assert set(NOT_A_REQUEST) <= raws, set(NOT_A_REQUEST) - raws
