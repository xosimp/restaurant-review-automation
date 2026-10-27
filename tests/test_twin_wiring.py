"""The web and phone twins are wired by name; this resolves every name
(audit #25 A04-03 / R21).

About 150 web routes reach their phone body by string — client_api's
`_m("mobile_x")` does `getattr(mobile_api, name)` and strips ONE decorator
(`__wrapped__`) — and mobile_api reaches back into client_api through
`_capi.<name>` (and `_capi.<route>.__wrapped__`). A rename passed py_compile
and every test and turned the web half into a 500; a decorator added ABOVE
`@mobile_login_required` would be the one stripped, and the web call would
then run the Bearer check against a cookie session (every web user a 401).
The old guards were source-text asserts that the string is present.

This file:
- resolves every `_m("…")` to a function in mobile_api whose OUTERMOST
  wrapper is `mobile_login_required` (and which is not wrapped by it twice);
- resolves every `_capi.<name>` mobile_api uses, and checks each
  `_capi.<route>.__wrapped__` strips exactly `login_required`;
- calls every web route that delegates through `_m` once, through the real
  client_bp as a signed-in owner, and asserts it does not 500 (31 of them
  were referenced by no test at all).
"""
import ast
import os
import re
import socket

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CSRF = {"X-CSRF": "twin-csrf"}


def _tree(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _m_names():
    """{mobile function name: [client_api functions that delegate to it]}."""
    out = {}
    for fn in ast.walk(_tree("client_api.py")):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_m"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.setdefault(node.args[0].value, []).append(fn.name)
    return out


def _capi_uses():
    """(attribute names used as `_capi.<name>`, names used as `_capi.<name>.__wrapped__`)."""
    names, wrapped = set(), set()
    for node in ast.walk(_tree("mobile_api.py")):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "_capi":
            names.add(node.attr)
        if (isinstance(node, ast.Attribute) and node.attr == "__wrapped__" and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "_capi"):
            wrapped.add(node.value.attr)
    return names, wrapped


def _wrapper_code(decorator):
    """The code object every function the decorator wraps shares (its inner
    `decorated`): how a wrapper is recognised without reading source."""
    return decorator(lambda *a, **k: None).__code__


MOBILE_AUTH = _wrapper_code(auth.mobile_login_required)
WEB_AUTH = _wrapper_code(auth.login_required)


# ── the names resolve, and the stripped decorator is the auth one ───────────

def test_the_scan_finds_the_delegations():
    """Guards the guard: a scan that matched nothing would pass below."""
    names = _m_names()
    assert len(names) >= 140, len(names)
    assert "mobile_verify_2fa_setup" in names or any("2fa" in n for n in names)
    used, wrapped = _capi_uses()
    assert len(used) >= 50 and wrapped >= {"home_dismiss_api", "home_assign_api", "dismiss_onboarding"}


@pytest.mark.parametrize("name", sorted(_m_names()))
def test_every_web_to_phone_name_resolves_to_a_bearer_guarded_view(name):
    fn = getattr(mobile_api, name, None)
    assert callable(fn), f"client_api delegates to mobile_api.{name}, which does not exist ({_m_names()[name]})"
    assert getattr(fn, "__code__", None) is MOBILE_AUTH, (
        f"mobile_api.{name}'s outermost decorator is not @mobile_login_required: client_api._m strips the "
        f"outermost one, so the web call would run whatever is under it")
    inner = getattr(fn, "__wrapped__", None)
    assert inner is not None and getattr(inner, "__code__", None) is not MOBILE_AUTH, \
        f"mobile_api.{name} is wrapped by mobile_login_required twice: the web call would still check a Bearer token"
    assert client_api._m(name) is inner


def test_the_check_sees_a_decorator_stacked_above_the_auth_one():
    """The hazard itself: a rate limiter (any functools.wraps decorator)
    added above @mobile_login_required is what _m would strip."""
    import functools

    def limiter(f):
        @functools.wraps(f)
        def limited(*a, **k):
            return f(*a, **k)
        return limited

    def body(current_user):
        return current_user
    guarded = auth.mobile_login_required(body)
    assert guarded.__code__ is MOBILE_AUTH
    assert limiter(guarded).__code__ is not MOBILE_AUTH
    assert auth.mobile_login_required(guarded).__wrapped__.__code__ is MOBILE_AUTH      # wrapped twice


def test_every_capi_name_the_phone_uses_exists():
    used, _ = _capi_uses()
    missing = sorted(n for n in used if not hasattr(client_api, n))
    assert missing == [], f"mobile_api calls client_api names that do not exist: {missing}"


@pytest.mark.parametrize("name", sorted(_capi_uses()[1]))
def test_every_capi_route_the_phone_unwraps_is_login_required_outermost(name):
    fn = getattr(client_api, name)
    assert getattr(fn, "__code__", None) is WEB_AUTH, (
        f"client_api.{name}'s outermost decorator is not @login_required: mobile_api strips it with __wrapped__")
    assert getattr(fn.__wrapped__, "__code__", None) is not WEB_AUTH


# ── every delegating web route answers without a 500 ────────────────────────
#
# Each delegating web route is called once as a signed-in owner of a
# restaurant of its own. A write gets a REALISTIC body — the keys its view
# (and the functions it hands the body to, three levels deep) reads from the
# JSON, each with a value of the type the code treats it as — not `{}`,
# which most writes answer with a 400 before reaching the body (blind
# re-audit 10/27/26, B4 "the twin smoke counts a handled 500 as a pass").
# Then it fails on:
#   - an unhandled exception (a renamed body is an AttributeError, a
#     mis-stripped decorator a TypeError) and a 401 (the session never
#     reached the body);
#   - ANY 5xx, handled or not: 47 of the bodies catch Exception and answer
#     500 themselves, so a renamed helper INSIDE one was invisible — unless
#     the route is in ALLOWED_5XX, which says why (a provider or the model,
#     which the suite does not have);
#   - an AttributeError, NameError, ImportError or a signature TypeError a
#     body caught and reported (ops.capture, or a log record with its
#     traceback) — wiring breaks, never the input's fault.

_GET_JSON_TYPES = {"str": "", "int": 1, "float": 1.0, "bool": True, "list": [], "dict": {}}
_STR_METHODS = {"strip", "lower", "upper", "split", "startswith", "endswith", "replace", "lstrip", "rstrip",
                "isdigit", "title", "encode", "casefold"}
_DICT_METHODS = {"get", "items", "keys", "values", "setdefault"}
_DEFS = {}


def _module_defs(modname):
    """(top-level functions by name, {alias: module}, {name: (module, attr)})
    of an app module, or None when it is not one."""
    if modname not in _DEFS:
        base = os.path.join(ROOT, *modname.split("."))
        path = base + ".py" if os.path.exists(base + ".py") else os.path.join(base, "__init__.py")
        if not os.path.exists(path):
            _DEFS[modname] = None
        else:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            _DEFS[modname] = (defs,) + _imports(tree.body)
    return _DEFS[modname]


def _imports(nodes):
    """({alias: module}, {name: (module, attr)}) of the imports in `nodes`
    (inside if/try blocks too, never inside nested functions)."""
    mods, names, stack = {}, {}, list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Import):
            for a in n.names:
                mods[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                names[a.asname or a.name] = (n.module, a.name)
        elif isinstance(n, (ast.If, ast.Try, ast.With)):
            stack.extend(ast.iter_child_nodes(n))
    return mods, names


def _is_body(node):
    """request.get_json(...), `request.get_json(...) or {}`, request.json."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _is_body(node.values[0])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_json":
        return True
    return isinstance(node, ast.Attribute) and node.attr == "json" and getattr(node.value, "id", "") == "request"


def _callee(modname, fn, call):
    """(module, function node) an app function call reaches, else None."""
    info = _module_defs(modname)
    if info is None:
        return None
    defs, mods, names = info
    lmods, lnames = _imports(fn.body)
    mods, names = {**mods, **lmods}, {**names, **lnames}
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "__wrapped__":
        f = f.value
    if isinstance(f, ast.Name):
        if f.id in defs:
            return modname, defs[f.id]
        if f.id in names:
            sub = _module_defs(names[f.id][0])
            if sub and names[f.id][1] in sub[0]:
                return names[f.id][0], sub[0][names[f.id][1]]
    elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in mods:
        sub = _module_defs(mods[f.value.id])
        if sub and f.attr in sub[0]:
            return mods[f.value.id], sub[0][f.attr]
    return None


def _literal_type(node):
    if isinstance(node, ast.Constant) and node.value is not None:
        return type(node.value).__name__
    if isinstance(node, (ast.List, ast.Tuple, ast.ListComp)):
        return "list"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("list", "dict", "set", "tuple"):
        return "dict" if node.func.id == "dict" else "list"
    return None


def _uses(fn, exclude=()):
    """{local name: "str" | "dict" | "list"} from how `fn` uses each name:
    x.strip() says str, x.items() or (x or {}).items() dict, a loop over it
    a list."""
    used_as = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.BoolOp) and isinstance(node.func.value.values[0], ast.Name):
            fallback = next((_literal_type(v) for v in node.func.value.values[1:] if _literal_type(v)), None)
            if fallback:
                used_as.setdefault(node.func.value.values[0].id, fallback)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            m = node.func.attr
            if m in _STR_METHODS:
                used_as.setdefault(node.func.value.id, "str")
            elif m in _DICT_METHODS and node.func.value.id not in exclude:
                used_as.setdefault(node.func.value.id, "dict")
            elif m in ("append", "extend", "sort"):
                used_as.setdefault(node.func.value.id, "list")
        elif isinstance(node, ast.For) and isinstance(node.iter, ast.Name):
            used_as.setdefault(node.iter.id, "list")
    return used_as


def body_keys(modname, fn, data_names=frozenset(), depth=0, seen=None):
    """{key: type name or None} for every key the view `fn` (and what it
    hands the body to) reads from the request's JSON body: `data.get("k")`,
    `data["k"]`, `"k" in data`. The type is read from how the code treats
    the value — `.get("k", 0)`, `(data.get("k") or "").strip()`,
    `int(data.get("k"))`, `x = data.get("k")` then `x.items()`."""
    seen = set() if seen is None else seen
    sig = (modname, fn.name, tuple(sorted(data_names)))
    if sig in seen or depth > 3:
        return {}
    seen.add(sig)
    names = set(data_names)
    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, ast.Assign) and _is_body(node.value):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    def is_data(n):
        return (isinstance(n, ast.Name) and n.id in names) or _is_body(n)

    used_as = _uses(fn, exclude=names)

    def type_of(read):
        """What the code around the read treats its value as."""
        kind = _literal_type(read.args[1]) if isinstance(read, ast.Call) and len(read.args) > 1 else None
        up = parents.get(read)
        if isinstance(up, ast.BoolOp) and isinstance(up.op, ast.Or):
            kind = kind or next((_literal_type(v) for v in up.values[1:] if _literal_type(v)), None)
            read, up = up, parents.get(up)
        if isinstance(up, ast.Call) and up.args and up.args[0] is read and getattr(up.func, "id", None) in _GET_JSON_TYPES:
            return up.func.id
        if isinstance(up, ast.Call) and (read in up.args or any(k.value is read for k in up.keywords)):
            # Handed to an app function: how that function uses its parameter.
            target = _callee(modname, fn, up)
            if target is not None:
                params = [p.arg for p in target[1].args.posonlyargs + target[1].args.args + target[1].args.kwonlyargs]
                pname = params[up.args.index(read)] if read in up.args and up.args.index(read) < len(params) else \
                    next((k.arg for k in up.keywords if k.value is read), None)
                kind = kind or _uses(target[1]).get(pname)
        if isinstance(up, ast.Attribute) and isinstance(parents.get(up), ast.Call):
            if up.attr in _STR_METHODS:
                return "str"
            if up.attr in _DICT_METHODS:
                return "dict"
        if isinstance(up, ast.Assign) and len(up.targets) == 1 and isinstance(up.targets[0], ast.Name):
            kind = kind or used_as.get(up.targets[0].id)
        return kind

    keys = {}
    for node in ast.walk(fn):
        key = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop") \
                and is_data(node.func.value) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            key = node.args[0].value
        elif isinstance(node, ast.Subscript) and is_data(node.value) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            key = node.slice.value
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In) \
                and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) \
                and is_data(node.comparators[0]):
            keys.setdefault(node.left.value, None)
        if key is not None:
            kind = type_of(node)
            if keys.get(key) is None:
                keys[key] = kind
        if isinstance(node, ast.Call):
            handed = [i for i, a in enumerate(node.args) if is_data(a)]
            by_kw = [k.arg for k in node.keywords if k.arg and is_data(k.value)]
            target = _callee(modname, fn, node) if (handed or by_kw) else None
            if target is not None:
                params = [p.arg for p in target[1].args.posonlyargs + target[1].args.args]
                passed = {params[i] for i in handed if i < len(params)} | set(by_kw)
                for k, v in body_keys(target[0], target[1], frozenset(passed), depth + 1, seen).items():
                    if keys.get(k) is None:
                        keys[k] = v
    return keys


DAY = "2026-11-02"      # a fixed Monday: the body is the same on every run


def value_for(key, kind):
    """A plausible value of `kind` (or, unknown, of what the key's name says
    it holds)."""
    k = key.lower()
    if kind in ("list", "dict", "bool"):
        return {"list": [], "dict": {}, "bool": True}[kind]
    if kind in ("int", "float"):
        return 1
    if k == "place_id":
        return "ChIJtwinsmoke0000"
    if kind is None and (k == "id" or k.endswith("_id")):
        return 1
    if kind is None and (k == "ids" or k.endswith("_ids")):
        return [1]
    if "email" in k:
        return "twin-guest@x.test"
    if "phone" in k:
        return "+15555550123"
    if "password" in k:
        return "Twin-smoke-pass-2026!"
    if k in ("start", "end", "time") or k.endswith(("_start", "_end", "_time")):
        return "5:00pm"
    if "date" in k or k in ("day", "week", "week_start", "week_of", "as_of", "since", "until"):
        return DAY
    if "url" in k or "link" in k:
        return "https://example.com/twin"
    if kind is None and (k.startswith(("is_", "has_", "include", "allow", "enable", "dry")) or
                         k in ("enabled", "active", "confirm", "confirmed", "dismissed", "force")):
        return True
    if kind is None and any(w in k for w in ("amount", "price", "cost", "rate", "hours", "qty", "quantity", "count",
                                             "pct", "percent", "target", "budget", "minutes", "score", "rating",
                                             "limit", "cents", "wage", "dollars", "index")):
        return 1
    if "role" in k:
        return "Server"
    if any(w in k for w in ("employee", "staff", "person", "member")):
        return "Twin Staff"
    if "code" in k or k == "pin":
        return "123456"
    return "Twin smoke"


_DELEGATES = {}
for _mname, _clients in _m_names().items():
    for _c in _clients:
        _DELEGATES.setdefault(_c, []).append(_mname)

# A body the reader cannot infer, set by hand — each with why.
BODY_OVERRIDES = {}

# Routes whose 5xx is the truth in a suite with no provider keys and no
# model: never wiring. Each says why; keep it short.
ALLOWED_5XX = {
    "client.account_2fa_send_test": "emails the code through Resend; no key in the suite (502 'email delivery failed')",
    "client.account_export_data": "emails the export through Resend; no key in the suite (502)",
    "client.guest_campaign_draft": "drafts with the model; no key in the suite (AINotConfigured, a handled 500)",
    "client.guest_newsletter_draft_api": "drafts with the model; no key in the suite (AINotConfigured, a handled 500)",
}


def realistic_body(endpoint):
    """The JSON a real client sends this write: every key its view reads,
    each a value of the type the code treats it as."""
    if endpoint in BODY_OVERRIDES:
        return dict(BODY_OVERRIDES[endpoint])
    name = endpoint.split(".", 1)[1]
    keys = {}
    cdefs = _module_defs("client_api")[0]
    if name in cdefs:
        keys.update(body_keys("client_api", cdefs[name]))
    mdefs = _module_defs("mobile_api")[0]
    for mname in _DELEGATES.get(name, ()):
        if mname in mdefs:
            for k, v in body_keys("mobile_api", mdefs[mname]).items():
                if keys.get(k) is None:
                    keys[k] = v
    return {k: value_for(k, v) for k, v in sorted(keys.items())}


def _delegating_endpoints():
    """{client_bp endpoint: rule} for every web route whose view delegates
    through _m."""
    delegators = {d for ds in _m_names().values() for d in ds}
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(client_api.client_bp)
    out = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("client.") and rule.endpoint.split(".", 1)[1] in delegators:
            out[rule.endpoint] = rule
    return out


_ENDPOINTS = _delegating_endpoints()
_OWNERS = [0]


@pytest.fixture
def owner_client(module_db_path, monkeypatch):
    """A signed-in owner of a restaurant of their own, in the module's one
    database (a fresh copy per route cost the Postgres gate 3.5 minutes of
    template clones; a restaurant per route keeps the routes apart)."""
    db_path = module_db_path
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)

    def _blocked(*a, **k):
        raise OSError("network blocked in the twin smoke")
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: _blocked())
    for k in ("ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "GOOGLE_PLACES_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "")
    _OWNERS[0] += 1
    n = _OWNERS[0]
    rid = models.create_restaurant(models.Restaurant(name=f"Twin Co {n}", owner_email=f"twin{n}@x.test",
                                                     service_tier="full", billing_status="active", module_reviews=1,
                                                     module_labor=1, module_inventory=1, module_marketing=1),
                                   db_path=db_path)
    uid = auth.create_user(rid, f"twinowner{n}", f"twin{n}@x.test", "twin-owner-pass", db_path=db_path)
    auth.upsert_membership(uid, rid, "client", db_path=db_path)
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    return c


_WIRING = (AttributeError, NameError, ImportError, UnboundLocalError)
_SIGNATURE = re.compile(r"unexpected keyword argument|required (?:positional|keyword-only) argument|"
                        r"positional arguments? but \d+ (?:was|were) given|got multiple values for argument")


def _wiring_break(exc):
    return isinstance(exc, _WIRING) or (isinstance(exc, TypeError) and bool(_SIGNATURE.search(str(exc))))


def smoke(client, endpoint):
    """[what went wrong] calling `endpoint` once per method as the client's
    owner — empty when the route answered as wired."""
    import logging
    import ops
    from flask import got_request_exception
    rule = _ENDPOINTS[endpoint]
    crashed, caught = [], []

    def _record(sender, exception, **extra):
        crashed.append(f"{type(exception).__name__}: {exception}")

    class _Caught(logging.Handler):
        def emit(self, record):
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None and _wiring_break(exc):
                caught.append(f"logged {type(exc).__name__}: {exc}")

    real_capture = ops.capture

    def _capture(e, *a, **k):
        if _wiring_break(e):
            caught.append(f"ops.capture-d {type(e).__name__}: {e}")
        return real_capture(e, *a, **k)

    handler = _Caught(level=logging.DEBUG)
    root = logging.getLogger()
    got_request_exception.connect(_record, client.application)
    root.addHandler(handler)
    ops.capture = _capture
    problems = []
    path = re.sub(r"<(?:int:)?[^>]+>", lambda m: "1" if m.group(0).startswith("<int:") else "x", rule.rule)
    try:
        for method in sorted(set(rule.methods or ()) & {"GET", "POST", "PUT", "PATCH", "DELETE"}):
            if method == "GET":
                resp = client.get(path, headers=CSRF)
            else:
                resp = client.open(path, method=method, json=realistic_body(endpoint), headers=CSRF)
            where = f"{method} {rule.rule}"
            if crashed:
                problems.append(f"{where} raised: {crashed}")
            if caught:
                problems.append(f"{where} caught a wiring break: {caught}")
            if resp.status_code == 401:
                problems.append(f"{where} -> 401: the web session did not reach the body")
            if resp.status_code >= 500 and endpoint not in ALLOWED_5XX:
                problems.append(f"{where} -> {resp.status_code} (a handled 5xx is a failure here unless "
                                f"ALLOWED_5XX says why): {resp.get_data(as_text=True)[:200]}")
            del crashed[:], caught[:]
    finally:
        ops.capture = real_capture
        root.removeHandler(handler)
        got_request_exception.disconnect(_record, client.application)
    return problems


def test_the_smoke_covers_the_delegating_routes():
    assert len(_ENDPOINTS) >= 140, len(_ENDPOINTS)


def test_the_allowances_name_routes_that_exist():
    assert set(ALLOWED_5XX) <= set(_ENDPOINTS), sorted(set(ALLOWED_5XX) - set(_ENDPOINTS))
    assert set(BODY_OVERRIDES) <= set(_ENDPOINTS), sorted(set(BODY_OVERRIDES) - set(_ENDPOINTS))


@pytest.mark.parametrize("endpoint", sorted(_ENDPOINTS))
def test_every_delegating_web_route_answers_without_a_500(owner_client, endpoint):
    problems = smoke(owner_client, endpoint)
    assert not problems, "\n".join(problems)


# ── the smoke itself sees what it is for ─────────────────────────────────────

def _endpoint_of(mobile_name):
    delegators = set(_m_names().get(mobile_name, ()))
    hits = [e for e in _ENDPOINTS if e.split(".", 1)[1] in delegators]
    assert hits, mobile_name
    return hits[0]


def test_a_renamed_helper_inside_a_body_that_catches_everything_fails_the_smoke(owner_client, monkeypatch):
    """mobile_bar_counts catches Exception and answers a handled 500; a
    helper renamed under it used to pass the smoke."""
    import bar.counts
    endpoint = _endpoint_of("mobile_bar_counts")
    assert smoke(owner_client, endpoint) == []
    monkeypatch.delattr(bar.counts, "next_due")
    problems = smoke(owner_client, endpoint)
    assert problems and any("500" in p or "AttributeError" in p for p in problems), problems


def test_a_write_gets_the_keys_its_body_reads_with_their_types():
    body = realistic_body(_endpoint_of("mobile_add_competitor"))
    assert isinstance(body.get("place_id"), str) and body["place_id"]
    body = realistic_body(_endpoint_of("mobile_bar_count_save"))
    assert body["count_date"] == DAY and isinstance(body["values"], dict)
    body = realistic_body(_endpoint_of("mobile_save_shift_profile"))
    assert isinstance(body.get("profile"), dict), body
