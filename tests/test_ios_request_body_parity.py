"""Every iOS write sends every key its server route reads (parity audit #24).

Five Critical parity bugs had one root cause: a hand-written Swift
`Encodable` request body left out a key the server reads, so the server
silently did something else (auto-approve without `include_4star`, a
Facebook post without `media_id`, `count` where the route read `eligible`).
No test could see it: the Swift side compiled, the server side defaulted.

This test reads both halves from source and compares them:

- **Swift.** Every call under `ios/CavnarAI` (all targets, not the tests)
  that names a write `method:` — `APIClient.send`, `sendKeepingBody`,
  `sendWithHeaders`, `sendWithBearer`, `sendUnauthenticated`, the offline
  queues' `QueuedWrite`/`enqueue`, the staff tier's `authed`, and any view
  model's own `post(_:body:)` — is followed through wrapper functions to the
  call that names a path, and its body expression is resolved to the JSON
  keys it encodes: a struct's stored properties (CodingKeys raw values when
  declared; a custom `encode(to:)`'s `forKey:` cases), a dictionary literal's
  keys, a local `var body: [String: …]` and its `body["k"] = …` writes.
  `JSONEncoder.cavnar` uses the default key strategy (no
  `convertToSnakeCase`), which `test_the_encoder_has_no_key_strategy` pins.
- **Server.** A subprocess imports `hosted_dashboard` (importing it here
  would wire CSRF onto client_bp for every later test in this worker) and
  reads, for every write rule under `/mobile/` and `/staff/`, the top-level
  keys its view reads from the JSON body — `data.get("k")`, `data["k"]`,
  `"k" in data`, `request.get_json()…`, a `_body()` helper — following the
  functions the body is passed to and the ones that reach `request`
  themselves (a mobile twin calling its web route, a `_do_*` body), three
  levels deep.

A key the server reads and no iOS caller of that route sends FAILS, unless
it is in `NOT_SENT_BY_IOS` with the reason. A key the phone sends that the
route never reads is a warning — and a failure when it is one edit away
from a key the route does read (a misspelling).
"""
import ast
import difflib
import functools
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
PREFIXES = ("/mobile/", "/staff/")


# ═════════════════════════════════════════════════════════════════════════════
# Server: the keys each write route reads
# ═════════════════════════════════════════════════════════════════════════════

def _is_project_fn(obj):
    if not isinstance(obj, types.FunctionType):
        return False
    f = getattr(obj.__code__, "co_filename", "") or ""
    return f.startswith(ROOT) and "site-packages" not in f


_AST = {}
_SCOPES = {}


def _fn_ast(fn):
    if fn not in _AST:
        node = None
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            node = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        except (OSError, TypeError, SyntaxError):
            pass
        _AST[fn] = node
    return _AST[fn]


def _is_request_json(node, names=("request",)):
    """`request.get_json(...)` or `request.json` — `names` are the names
    Flask's request goes by in that function (`from flask import request
    as req`)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_json" \
            and isinstance(node.func.value, ast.Name) and node.func.value.id in names:
        return True
    return isinstance(node, ast.Attribute) and node.attr == "json" \
        and isinstance(node.value, ast.Name) and node.value.id in names


class _Scope:
    """The names a function body can reach: its globals, its closure cells,
    and the imports it makes inside its own body (most cross-module calls
    here are lazy imports)."""

    def __init__(self, fn, node):
        self.names = dict(fn.__globals__)
        for name, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
            try:
                self.names[name] = cell.cell_contents
            except ValueError:
                pass
        for sub in ast.walk(node):
            try:
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        top = a.name.split(".")[0]
                        self.names[a.asname or top] = importlib.import_module(a.name if a.asname else top)
                elif isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                    mod = importlib.import_module(sub.module)
                    for a in sub.names:
                        if hasattr(mod, a.name):
                            self.names[a.asname or a.name] = getattr(mod, a.name)
            except Exception:  # noqa: BLE001 — an import that fails here fails in production too
                pass

    def resolve(self, func):
        """A dotted name (`_do_x`, `client_api._do_x`,
        `_capi.home_assign_api.__wrapped__`) to the project function it
        names, unwrapped; None for anything else."""
        chain = []
        while isinstance(func, ast.Attribute):
            chain.append(func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        obj = self.names.get(func.id)
        for attr in reversed(chain):
            if not isinstance(obj, (types.ModuleType, types.FunctionType)):
                return None
            obj = getattr(obj, attr, None)
        try:
            obj = inspect.unwrap(obj) if callable(obj) else obj
        except ValueError:
            return None
        return obj if _is_project_fn(obj) else None


def _scope(fn, node):
    if fn not in _SCOPES:
        _SCOPES[fn] = _Scope(fn, node)
    return _SCOPES[fn]


@functools.lru_cache(maxsize=None)
def _is_body_helper(fn):
    """A no-argument function whose return value is the request body
    (strategy_routes._body, staff_knowledge_routes._body, ...)."""
    node = _fn_ast(fn)
    if node is None or node.args.args:
        return False
    return any(isinstance(s, ast.Return) and s.value is not None
               and any(_is_request_json(x) for x in ast.walk(s.value)) for s in ast.walk(node))


_MAX_DEPTH = 3
_MEMO = {}
_ACTIVE = set()


def _reads(fn, params=frozenset(), depth=0):
    """(keys, dynamic, followed) for one function: the body keys it reads
    from `request` or from the parameters named in `params`, and from the
    functions it hands the body to or that reach `request` themselves."""
    memo_key = (fn, params, depth)
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    if memo_key in _ACTIVE or depth > _MAX_DEPTH:
        return {}, (), (), (), frozenset()
    node = _fn_ast(fn)
    if node is None:
        return {}, (), (), (), frozenset()
    _ACTIVE.add(memo_key)
    scope = _scope(fn, node)
    body = set(params)
    keys, dynamic, followed, aliases = {}, [], [f"{fn.__module__}.{fn.__qualname__}"], []
    args_keys = set()
    where = f"{os.path.relpath(fn.__code__.co_filename, ROOT)}:"
    first = fn.__code__.co_firstlineno - 1

    import flask
    req_names = {"request"} | {n for n, v in scope.names.items() if v is flask.request}
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and sub.module == "flask":
            req_names |= {a.asname or a.name for a in sub.names if a.name == "request"}

    def is_body(expr):
        if isinstance(expr, ast.Name):
            return expr.id in body
        if _is_request_json(expr, req_names):
            return True
        if isinstance(expr, ast.BoolOp):
            return any(is_body(v) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return is_body(expr.body) or is_body(expr.orelse)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name) and expr.func.id == "dict" and expr.args:
                return is_body(expr.args[0])
            target = scope.resolve(expr.func)
            return target is not None and not expr.args and _is_body_helper(target)
        return False

    def bind_names():
        """Names bound to the body, to a fixpoint (aliases, `data = data or {}`)."""
        changed = True
        while changed:
            changed = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    targets, value = sub.targets, sub.value
                elif isinstance(sub, (ast.AnnAssign, ast.NamedExpr)) and sub.value is not None:
                    targets, value = [sub.target], sub.value
                else:
                    continue
                if is_body(value):
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id not in body:
                            body.add(t.id)
                            changed = True

    bind_names()

    def call_target(sub):
        target = scope.resolve(sub.func)
        if target is None or target is fn:
            return None, None
        try:
            return target, list(inspect.signature(target).parameters)
        except (TypeError, ValueError):
            return None, None

    # A callback handed the body: `_brief_write(u, lambda sb, day, b: b.get("item"))`
    # — the callee calls its callable parameter with the body it read.
    nested = {n.name: n for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n is not node}
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        target, names = call_target(sub)
        if target is None:
            continue
        cbs = _reads(target, frozenset(), depth + 1)[4]
        for pname, pos in cbs:
            arg = None
            if pname in names and names.index(pname) < len(sub.args):
                arg = sub.args[names.index(pname)]
            for kw in sub.keywords:
                if kw.arg == pname:
                    arg = kw.value
            if isinstance(arg, ast.Name) and arg.id in nested:
                arg = nested[arg.id]
            if isinstance(arg, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)) and pos < len(arg.args.args):
                body.add(arg.args.args[pos].arg)
    bind_names()

    # This function's own callbacks: a call of one of its parameters with
    # the body as an argument.
    own = set(fn.__code__.co_varnames[:fn.__code__.co_argcount])
    callbacks = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in own \
                and sub.func.id not in body:
            for i, a in enumerate(sub.args):
                if is_body(a):
                    callbacks.add((sub.func.id, i))

    # Loop variables over constant tuples: `for k in ("a", "b"): data.get(k)`.
    consts = {}
    for sub in ast.walk(node):
        if isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(sub.target, ast.Name) \
                and isinstance(sub.iter, (ast.Tuple, ast.List, ast.Set)):
            vals = [e.value for e in sub.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if vals and len(vals) == len(sub.iter.elts):
                consts.setdefault(sub.target.id, set()).update(vals)

    def key_of(k, line):
        loc = f"{where}{first + line}"
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.setdefault(k.value, loc)
        elif isinstance(k, ast.Name) and k.id in consts:
            for c in consts[k.id]:
                keys.setdefault(c, loc)
        else:
            dynamic.append(f"{fn.__module__}.{fn.__qualname__}:{line}")

    def merge(target, passed):
        k, d, f, al, _cb = _reads(target, frozenset(passed), depth + 1)
        for kk, loc in k.items():
            if kk == "\x00args":
                args_keys.update(loc)
                continue
            keys.setdefault(kk, loc)
        dynamic.extend(d)
        followed.extend(f)
        aliases.extend(al)

    def direct_key(expr):
        """"k" when expr is `body.get("k"…)` / `body["k"]`."""
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get" \
                and expr.args and is_body(expr.func.value) and isinstance(expr.args[0], ast.Constant):
            return expr.args[0].value
        if isinstance(expr, ast.Subscript) and is_body(expr.value) and isinstance(expr.slice, ast.Constant):
            return expr.slice.value
        return None

    # Alternatives the route accepts for one value: `b.get("a") or b.get("b")`
    # and `b.get("a", b.get("b"))` — sending either one is sending it.
    for sub in ast.walk(node):
        group = set()
        if isinstance(sub, ast.BoolOp) and isinstance(sub.op, ast.Or):
            group = {direct_key(v) for v in sub.values} - {None}
        elif isinstance(sub, ast.Call) and direct_key(sub) and len(sub.args) > 1:
            group = {direct_key(sub), direct_key(sub.args[1])} - {None}
        if len(group) > 1:
            aliases.append(frozenset(group))

    for sub in ast.walk(node):
        # `request.args.get("k")`: a key the route also takes in the query
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get" \
                and isinstance(sub.func.value, ast.Attribute) and sub.func.value.attr == "args" \
                and isinstance(sub.func.value.value, ast.Name) and sub.func.value.value.id in req_names \
                and sub.args and isinstance(sub.args[0], ast.Constant):
            args_keys.add(sub.args[0].value)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr in ("get", "pop", "setdefault") and sub.args and is_body(sub.func.value):
            key_of(sub.args[0], sub.lineno)
        elif isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Load) and is_body(sub.value):
            key_of(sub.slice, sub.lineno)
        elif isinstance(sub, ast.Compare) and len(sub.ops) == 1 and isinstance(sub.ops[0], (ast.In, ast.NotIn)) \
                and is_body(sub.comparators[0]):
            key_of(sub.left, sub.lineno)
        if not isinstance(sub, ast.Call):
            continue
        target, names = call_target(sub)
        if target is None:
            continue
        passed = {names[i] for i, a in enumerate(sub.args) if i < len(names) and is_body(a)}
        passed |= {kw.arg for kw in sub.keywords if kw.arg and is_body(kw.value)}
        if passed or depth < _MAX_DEPTH:
            merge(target, passed)
    # A closure's wrapped body (strategy_routes._wrap's `body`, _idempotent's).
    for cell in fn.__closure__ or ():
        try:
            obj = cell.cell_contents
        except ValueError:
            continue
        if _is_project_fn(obj) and obj is not fn:
            merge(inspect.unwrap(obj), set())

    _ACTIVE.discard(memo_key)
    if args_keys:
        keys["\x00args"] = sorted(args_keys)      # carried up with the keys, split out below
    out = (keys, tuple(dynamic), tuple(followed), tuple(aliases), frozenset(callbacks))
    _MEMO[memo_key] = out
    return out


def server_reads(app):
    """Every write rule under the iOS prefixes, with the top-level body keys
    its view reads."""
    out = []
    for rule in app.url_map.iter_rules():
        methods = sorted(set(rule.methods or ()) & set(WRITE_METHODS))
        if not methods or not rule.rule.startswith(PREFIXES):
            continue
        keys, dynamic, followed, aliases, _ = _reads(inspect.unwrap(app.view_functions[rule.endpoint]))
        keys = dict(keys)
        args_keys = keys.pop("\x00args", [])
        out.append({"rule": rule.rule, "methods": methods, "endpoint": rule.endpoint, "args_keys": args_keys,
                    "keys": sorted(keys), "where": keys,
                    "aliases": sorted(sorted(a) for a in set(aliases)), "dynamic": list(dict.fromkeys(dynamic))[:5],
                    "followed": list(dict.fromkeys(followed))[:20]})
    return out


def _server_main(out_path):
    """Runs in a subprocess: a throwaway volume, no provider keys, no
    scheduler, no .env — then the app's url_map and each route's reads."""
    vol = tempfile.mkdtemp(prefix="cavnar-bodyparity-")
    import sqlite3
    sqlite3.connect(os.path.join(vol, "reviews.db")).close()   # pre-create: no legacy adoption
    for k in list(os.environ):
        if k.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_AUTH_TOKEN")):
            os.environ.pop(k)
    os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", SECRET_KEY="test-secret",
                      CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import hosted_dashboard
    try:
        with open(out_path, "w") as fh:
            json.dump(server_reads(hosted_dashboard.app), fh)
    finally:
        import shutil
        shutil.rmtree(vol, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# Swift: every write call, its path and the keys its body encodes
# ═════════════════════════════════════════════════════════════════════════════

_LEX_STOP = re.compile(r'//|/\*|#+"|"')


def lex(src):
    """(masked, skel), both the length of `src`: `masked` has comments
    blanked; `skel` also has every string literal's contents (with its
    interpolations) blanked to `x`, so brackets can be matched on it.
    Newlines are kept in both, so offsets map to lines."""
    masked, skel = list(src), list(src)
    n = len(src)

    def blank(a, b, which):
        for t in range(a, b):
            if src[t] != "\n":
                for arr in which:
                    arr[t] = " " if arr is masked else "x"

    def string_end(i, term, raw):
        j = i
        while j < n:
            if src.startswith(term, j):
                return j
            if src[j] == "\\" and not raw:
                if j + 1 < n and src[j + 1] == "(":
                    depth, k = 1, j + 2
                    while k < n and depth:
                        if src[k] == '"':
                            t3 = '"""' if src.startswith('"""', k) else '"'
                            k = string_end(k + len(t3), t3, False) + len(t3)
                            continue
                        depth += {"(": 1, ")": -1}.get(src[k], 0)
                        k += 1
                    j = k
                    continue
                j += 2
                continue
            j += 1
        return n

    i = 0
    while True:
        m = _LEX_STOP.search(src, i)
        if not m:
            break
        tok, s = m.group(), m.start()
        if tok == "//":
            e = src.find("\n", s)
            e = n if e < 0 else e
            blank(s, e, (masked, skel))
            i = e
        elif tok == "/*":
            depth, k = 1, s + 2
            while k < n and depth:
                if src.startswith("/*", k):
                    depth, k = depth + 1, k + 2
                elif src.startswith("*/", k):
                    depth, k = depth - 1, k + 2
                else:
                    k += 1
            blank(s, k, (masked, skel))
            i = k
        else:
            hashes = tok[:-1]
            term = '"""' if src.startswith('"""', s + len(hashes)) else '"'
            start = s + len(hashes) + len(term)
            e = string_end(start, term + hashes, bool(hashes))
            for t in range(start, e):
                if src[t] != "\n":
                    skel[t] = "x"
            i = e + len(term) + len(hashes)
    return "".join(masked), "".join(skel)


def match_close(skel, i):
    """Index of the bracket closing the one at `i`."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for j in range(i, len(skel)):
        c = skel[j]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack.pop() != c:
                return -1
            if not stack:
                return j
    return -1


def split_top(skel, a, b):
    """Spans of the comma-separated items between a and b (exclusive)."""
    items, depth, start = [], 0, a
    for j in range(a, b):
        c = skel[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            items.append((start, j))
            start = j + 1
    if skel[start:b].strip():
        items.append((start, b))
    return items


class SwiftFile:
    def __init__(self, path, src):
        self.path = path
        self.rel = os.path.relpath(path, ROOT)
        self.src = src
        self.masked, self.skel = lex(src)
        self._lines = None
        self.funcs = []      # Func
        self.types = []      # TypeDecl

    def line(self, off):
        return self.src.count("\n", 0, off) + 1

    def text(self, a, b):
        return self.masked[a:b].strip()


class Expr:
    """A span of Swift source, resolved in its own file and function."""
    __slots__ = ("file", "a", "b")

    def __init__(self, file, a, b):
        while a < b and file.masked[a].isspace():
            a += 1
        while b > a and file.masked[b - 1].isspace():
            b -= 1
        self.file, self.a, self.b = file, a, b

    @property
    def text(self):
        return self.file.masked[self.a:self.b]

    @property
    def skel(self):
        return self.file.skel[self.a:self.b]


class Func:
    def __init__(self, file, name, start, params, body_a, body_b, private):
        self.file, self.name, self.start = file, name, start
        self.params = params        # [(label or None, internal, type_text, default Expr or None)]
        self.body_a, self.body_b = body_a, body_b
        self.private = private

    def param(self, name):
        for i, p in enumerate(self.params):
            if p[1] == name:
                return i, p
        return None, None


class TypeDecl:
    def __init__(self, file, kind, name, qual, a, b):
        self.file, self.kind, self.name, self.qual, self.a, self.b = file, kind, name, qual, a, b


_FUNC_RE = re.compile(r"\b(func\s+([A-Za-z_]\w*)|init\??)\s*(<[^{}()]*?>)?\s*\(")
_TYPE_RE = re.compile(r"\b(struct|class|enum|actor|extension|protocol)\s+([A-Za-z_][\w.]*)")
_PARAM_RE = re.compile(r"^\s*(?:@\w+\s+)*(?:(\w+)\s+)?(\w+)\s*:(.*)$", re.S)


def _index(f):
    skel = f.skel
    for m in _FUNC_RE.finditer(skel):
        p_open = m.end() - 1
        p_close = match_close(skel, p_open)
        if p_close < 0:
            continue
        body_open = skel.find("{", p_close)
        nxt = re.compile(r"[;\n]\s*(?:func|var|let|case|init|struct|class|enum|@)\b").search(skel, p_close)
        if body_open < 0 or (nxt and nxt.start() < body_open):
            continue      # a protocol requirement: no body
        body_close = match_close(skel, body_open)
        if body_close < 0:
            continue
        params = []
        for a, b in split_top(skel, p_open + 1, p_close):
            pm = _PARAM_RE.match(f.masked[a:b])
            if not pm:
                continue
            label, internal, rest = pm.group(1), pm.group(2), pm.group(3)
            if label is None:
                label = internal
            if label == "_":
                label = None
            eq = _top_eq(skel[a + pm.start(3):b])
            default = Expr(f, a + pm.start(3) + eq + 1, b) if eq >= 0 else None
            typ = rest[:eq] if eq >= 0 else rest
            params.append((label, internal, typ.strip(), default))
        line_start = skel.rfind("\n", 0, m.start()) + 1
        prefix = skel[line_start:m.start()]
        name = m.group(2) or "init"
        f.funcs.append(Func(f, name, m.start(), params, body_open, body_close,
                            "private" in prefix or "fileprivate" in prefix))
    stack = []
    for m in _TYPE_RE.finditer(skel):
        brace = skel.find("{", m.end())
        if brace < 0:
            continue
        between = skel[m.end():brace]
        if "\n\n" in between or "=" in between.split("where")[0] or "func " in between:
            continue
        close = match_close(skel, brace)
        if close < 0:
            continue
        while stack and stack[-1].b < m.start():
            stack.pop()
        name = m.group(2)
        parent = stack[-1].qual + "." if stack else ""
        qual = name if m.group(1) == "extension" else parent + name
        t = TypeDecl(f, m.group(1), name.split(".")[-1], qual, brace, close)
        f.types.append(t)
        stack.append(t)


def _top_eq(skel_text):
    depth = 0
    for j, c in enumerate(skel_text):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        elif c == "=" and depth == 0 and skel_text[j + 1:j + 2] != "=" and skel_text[j - 1:j] not in "!<>=":
            return j
    return -1


class Swift:
    """Every Swift file of the app's targets, indexed."""

    def __init__(self, files):
        self.files = files
        self.types_by_name = {}
        self.funcs_by_name = {}
        self.aliases = {}
        for f in files:
            _index(f)
            for m in re.finditer(r"\btypealias\s+(\w+)\s*=\s*([\w.]+)\s*$", f.skel, re.M):
                self.aliases.setdefault(m.group(1), m.group(2))
            for t in f.types:
                self.types_by_name.setdefault(t.name, []).append(t)
            for fn in f.funcs:
                self.funcs_by_name.setdefault(fn.name, []).append(fn)

    # ── lookups ──────────────────────────────────────────────────────────────

    def string_decls(self):
        """name -> [(file, match)] for every `let/var name [: String] =|{`."""
        if not hasattr(self, "_string_decls"):
            idx = {}
            pat = re.compile(r"\b(?:let|var)\s+(\w+)\s*(?::\s*String\s*)?(=|\{)")
            for f in self.files:
                for m in pat.finditer(f.skel):
                    idx.setdefault(m.group(1), []).append((f, m))
            self._string_decls = idx
        return self._string_decls

    def enclosing_func(self, f, off):
        best = None
        for fn in f.funcs:
            if fn.body_a < off < fn.body_b and (best is None or fn.body_a > best.body_a):
                best = fn
        return best

    def enclosing_type(self, f, off):
        best = None
        for t in f.types:
            if t.a < off < t.b and (best is None or t.a > best.a):
                best = t
        return best

    def find_type(self, name, f, off):
        """A type named `name` (`A.B` qualified or plain), preferring the
        one in scope at `f`/`off`."""
        last = name.split(".")[-1]
        cands = [t for t in self.types_by_name.get(last, []) if t.kind != "extension"]
        if not cands and last in self.aliases and self.aliases[last] != name:
            return self.find_type(self.aliases[last], f, off)
        if "." in name:
            qual = [t for t in cands if t.qual.endswith(name)]
            cands = qual or cands
        if len(cands) <= 1:
            return cands[0] if cands else None
        fn = self.enclosing_func(f, off)
        if fn is not None:
            local = [t for t in cands if t.file is f and fn.body_a < t.a < fn.body_b and t.a < off]
            if local:
                return local[-1]      # a function-local `struct Body`
        cands = [t for t in cands if not (self.enclosing_func(t.file, t.a) is not None
                                          and self.enclosing_type(t.file, t.a) is not None
                                          and self.enclosing_func(t.file, t.a).body_a > self.enclosing_type(t.file, t.a).a)]
        if len(cands) == 1:
            return cands[0]
        here = self.enclosing_type(f, off)
        while here is not None:
            for t in cands:
                if t.qual == here.qual + "." + last:
                    return t
            here = self.enclosing_type(f, here.a - 1) if here.a > 0 else None
        same = [t for t in cands if t.file is f and "." not in t.qual]
        if len(same) == 1:
            return same[0]
        same = [t for t in cands if t.file is f]
        if len(same) == 1:
            return same[0]
        top = [t for t in cands if "." not in t.qual]
        return top[0] if len(top) == 1 else None

    def type_parts(self, t):
        """The type's own declaration and every extension of it."""
        parts = [t]
        for e in self.types_by_name.get(t.name, []):
            if e.kind == "extension" and (e.qual == t.qual or e.qual == t.name):
                parts.append(e)
        return parts


# ── body keys of a type ──────────────────────────────────────────────────────

_PROP_RE = re.compile(r"(?:^|[\s;])((?:[\w()@]+\s+)*?)(let|var)\s+(\w+)\s*(:\s*([^=\n{;]+))?\s*(=|\{\}|;|$)", re.M)


_TOP = {}
_BRACE = re.compile(r"[{}]")


def _top_level(f, a, b):
    """The type body's own level: nested blocks collapsed to `{}`."""
    key = (f, a, b)
    if key not in _TOP:
        out, depth, last = [], 0, a + 1
        for m in _BRACE.finditer(f.skel, a + 1, b):
            if m.group() == "{":
                if depth == 0:
                    out.append(f.skel[last:m.start()])
                    out.append("{}")
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    last = m.end()
        if depth == 0:
            out.append(f.skel[last:b])
        _TOP[key] = "".join(out)
    return _TOP[key]


def _coding_keys(sw, t):
    """Every CodingKey enum nested in the type: case name -> raw value."""
    out = {}
    for part in sw.type_parts(t):
        for n in part.file.types:
            if n.kind == "enum" and part.a < n.a < part.b and n.qual.count(".") == part.qual.count(".") + 1:
                header = part.file.skel[n.a - 200 if n.a > 200 else 0:n.a]
                if "CodingKey" not in header.split("enum")[-1]:
                    continue
                text = part.file.masked[n.a + 1:n.b]
                for cm in re.finditer(r"\bcase\s+([^\n;]+)", text):
                    for item in cm.group(1).split(","):
                        im = re.match(r"\s*`?(\w+)`?\s*(?:=\s*\"([^\"]*)\")?", item)
                        if im:
                            out[im.group(1)] = im.group(2) or im.group(1)
    return out


def type_keys(sw, t):
    """(keys, note) a type encodes. note is None when fully understood."""
    ck = _coding_keys(sw, t)
    for part in sw.type_parts(t):
        for fn in part.file.funcs:
            if fn.name == "encode" and part.a < fn.start < part.b and fn.params and fn.params[0][0] == "to" \
                    and sw.enclosing_type(part.file, fn.start) is part:
                body = part.file.masked[fn.body_a:fn.body_b]
                used = {ck[c] for c in re.findall(r"\.(\w+)\b", body) if c in ck}
                lit = set(re.findall(r'(?:stringValue:|Key\()\s*"(\w+)"', body))
                if not used and not lit:
                    return set(), f"custom encode(to:) of {t.qual} encodes no named keys"
                return used | lit, None
    if ck:
        return set(ck.values()), None
    keys = set()
    top = _top_level(t.file, t.a, t.b)
    for m in _PROP_RE.finditer(top):
        mods, kind, name, _, typ, tail = m.groups()
        if re.search(r"\b(static|class|lazy)\b", mods or ""):
            continue
        if kind == "var" and tail == "{}":
            continue      # computed
        keys.add(name)
    return keys, None


# ── call sites ───────────────────────────────────────────────────────────────

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*(?:<[\w\s.,:\[\]?]*>)?\s*\(")
_METHOD_ARG = re.compile(r"\bmethod\s*:")
_WRITE_LIT = {"post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE",
              "get": "GET", "head": "HEAD"}


class Call:
    def __init__(self, file, name, a, b, args):
        self.file, self.name, self.a, self.b, self.args = file, name, a, b, args
        self.receiver, self.implicit = None, False

    def arg(self, label, position=None):
        for lab, e in self.args:
            if lab == label:
                return e
        if position is not None:
            pos = [e for lab, e in self.args if lab is None]
            if position < len(pos):
                return pos[position]
        return None


def _call_at(f, m):
    open_ = m.end() - 1
    close = match_close(f.skel, open_)
    if close < 0:
        return None
    args = []
    for a, b in split_top(f.skel, open_ + 1, close):
        lm = re.match(r"\s*([A-Za-z_]\w*)\s*:(?!:)", f.skel[a:b])
        if lm and not re.match(r"\s*\w+\s*:\s*$", f.skel[a:b]):
            args.append((lm.group(1), Expr(f, a + lm.end(), b)))
        else:
            args.append((None, Expr(f, a, b)))
    return Call(f, m.group(1), m.start(), close + 1, args)


def _calls_named(sw, name, files):
    """Every call of a function so named, with its receiver expression."""
    _CALLS = sw.__dict__.setdefault("_calls", {})
    if name not in _CALLS:
        pat = re.compile(r"\b(" + re.escape(name) + r")\s*(?:<[\w\s.,:\[\]?]*>)?\s*\(")
        out = []
        for f in sw.files:
            if name not in f.skel:
                continue
            for m in pat.finditer(f.skel):
                before = f.skel[max(0, m.start() - 120):m.start()]
                if re.search(r"\bfunc\s*$", before):
                    continue
                c = _call_at(f, m)
                if c is None:
                    continue
                rm = re.search(r"((?:[A-Za-z_]\w*[?!]?\.)*[A-Za-z_]\w*[?!]?)\.$", before) if before.endswith(".") else None
                c.receiver = Expr(f, m.start() - len(rm.group(0)), m.start() - 1) if rm else None
                c.implicit = before.endswith(".") and rm is None
                out.append(c)
        _CALLS[name] = out
    return [c for c in _CALLS[name] if c.file in files]


def _owner_names(sw, fn):
    t = sw.enclosing_type(fn.file, fn.start)
    return None if t is None else {t.name, t.qual.split(".")[-1]}


def _receiver_ok(sw, c, fn):
    """Whether call `c` can be a call of `fn`: its receiver's type (when
    known) is fn's type; a bare call sits inside fn's type."""
    owners = _owner_names(sw, fn)
    if owners is None:
        return c.receiver is None
    if c.receiver is None:
        if c.implicit:
            return True
        here = sw.enclosing_type(c.file, c.a)
        while here is not None:
            if here.name in owners:
                return True
            here = sw.enclosing_type(c.file, here.a - 1) if here.a > 0 else None
        return False
    typ = type_of(sw, c.receiver)
    if typ is None:
        return True
    return typ.split(".")[-1].split("<")[0] in owners


_NOT_CALLS = {"if", "guard", "while", "switch", "for", "return", "func", "init", "catch", "case"}


def seed_calls(sw):
    """Every call that names a `method:` argument."""
    for f in sw.files:
        for m in _METHOD_ARG.finditer(f.skel):
            # the innermost open paren before `method:`
            depth, j = 0, m.start() - 1
            while j >= 0:
                c = f.skel[j]
                if c in ")]}":
                    depth += 1
                elif c in "([{":
                    if depth == 0:
                        break
                    depth -= 1
                j -= 1
            if j < 0 or f.skel[j] != "(":
                continue
            cm = re.search(r"([A-Za-z_]\w*)\s*(?:<[\w\s.,:\[\]?]*>)?\s*$", f.skel[max(0, j - 80):j])
            if not cm or cm.group(1) in _NOT_CALLS:
                continue
            name = cm.group(1)
            if re.search(r"\bfunc\s+$", f.skel[max(0, j - 80):j][:cm.start()]):
                continue
            mm = re.compile("(" + re.escape(name) + r")\s*(?:<[\w\s.,:\[\]?]*>)?\s*\(").match(
                f.skel, max(0, j - 80) + cm.start())
            if not mm:
                continue
            c = _call_at(f, mm)
            if c and c.arg("method") is not None:
                yield c


# ── resolving expressions ────────────────────────────────────────────────────

class Unresolved(Exception):
    pass


class Carried(Exception):
    """The path is a lock-screen action's, read where the action is built
    (_carrier_writes)."""


class Param(Exception):
    """The expression is the enclosing function's parameter."""

    def __init__(self, func, name):
        super().__init__(name)
        self.func, self.name = func, name


_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def _strip(e):
    """Drop `try`, `await`, `try?`, a wrapping pair of parens, `as T`."""
    while True:
        t = e.skel
        m = re.match(r"\s*(try[?!]?|await)\s+", t)
        if m:
            e = Expr(e.file, e.a + m.end(), e.b)
            continue
        if t.startswith("(") and match_close(e.file.skel, e.a) == e.b - 1:
            e = Expr(e.file, e.a + 1, e.b - 1)
            continue
        m = re.search(r"\s+as[?!]?\s+[\w.\[\]: ]+$", t)
        if m and t.count("(") == t.count(")"):
            e = Expr(e.file, e.a, e.a + m.start())
            continue
        return e


def _local_decl(sw, e, name):
    """The expression a local `let/var name = …` (or `if/guard let`) in the
    enclosing function binds, the last one before `e`; or the declared type
    when there is only an annotation."""
    f = e.file
    fn = sw.enclosing_func(f, e.a)
    lo = fn.body_a if fn else 0
    hi = e.a
    pat = re.compile(r"\b(?:let|var)\s+" + re.escape(name) + r"\b\s*(?::\s*([^=\n{]+?))?\s*(=|\n|$)")
    found = None
    for m in pat.finditer(f.skel, lo, hi):
        found = m
    if found is None:
        return None, None, fn
    typ = (f.masked[found.start(1):found.end(1)].strip() if found.group(1) else None)
    if found.group(2) != "=":
        return None, typ, fn
    start = found.end()
    line_start = f.skel.rfind("\n", 0, found.start()) + 1
    before = f.skel[max(lo, found.start() - 400):found.start()].rstrip()
    cond = bool(re.search(r"\b(if|guard|while)\b", f.skel[line_start:found.start()])) or before.endswith(",")
    end = _stmt_end(f.skel, start, cond)
    return Expr(f, start, end), typ, fn


def _stmt_end(skel, start, cond=False):
    """Where the expression starting at `start` ends. `cond`: it is an
    `if let`/`guard let` binding, which also ends at `{` or `else`."""
    depth, j, n = 0, start, len(skel)
    while j < n:
        c = skel[j]
        if cond and depth == 0 and (c == "{" or (skel.startswith("else", j) and skel[j - 1] in " \n")):
            return j
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif c in ";" and depth == 0:
            return j
        elif c == "\n" and depth == 0:
            rest = skel[j + 1:j + 80].lstrip()
            prev = skel[start:j].rstrip()
            if not (rest[:1] in (".", "?", ":", "+", "&", "|") or prev.endswith(("=", "+", "?", ":", "&&", "||", ","))):
                return j
        elif c == "," and depth == 0:
            # `if let a = x, let b = y` — the binding ends at its comma
            return j
        j += 1
    return n


# Wrapper parameters bound to the caller's arguments while one chain of
# calls is resolved: id(Func) -> {param name: (Expr or None, type text)}.
_ENV = {}


def _param(sw, e, name):
    """The caller's expression (and the parameter's type) when `name` is a
    parameter of the enclosing function that a caller has bound; raises
    Param when it is an unbound parameter; None when it is not one."""
    fn = sw.enclosing_func(e.file, e.a)
    while fn is not None:
        idx, p = fn.param(name)
        if p is not None:
            if id(fn) in _ENV:
                bound, typ = _ENV[id(fn)][name]
                if bound is None:
                    raise Unresolved(f"{name}: no argument and no default")
                return bound, typ
            raise Param(fn, name)
        # a closure nested in a function sees the outer function's params
        fn = sw.enclosing_func(e.file, fn.start)
    return None


def resolve_string(sw, e, depth=0):
    """Every path template an expression can be: interpolations of
    non-path values become `{*}`."""
    if depth > 8:
        raise Unresolved(e.text)
    e = _strip(e)
    t, s = e.text, e.skel
    if not t:
        raise Unresolved("empty")
    # ternary
    q = _top_char(s, "?")
    if q > 0 and s[q - 1] == " ":
        colon = _top_char(s, ":", q)
        if colon > 0:
            return resolve_string(sw, Expr(e.file, e.a + q + 1, e.a + colon), depth + 1) + \
                resolve_string(sw, Expr(e.file, e.a + colon + 1, e.b), depth + 1)
    nc = s.find("??")
    if nc > 0:
        try:
            return resolve_string(sw, Expr(e.file, e.a, e.a + nc), depth + 1)
        except (Unresolved, Param):
            # `notice.mobileDismissPath ?? "/mobile/api/…"`: the literal fallback
            return resolve_string(sw, Expr(e.file, e.a + nc + 2, e.b), depth + 1)
    plus = _top_char(s, "+")
    if plus > 0:
        left = resolve_string(sw, Expr(e.file, e.a, e.a + plus), depth + 1)
        try:
            right = resolve_string(sw, Expr(e.file, e.a + plus + 1, e.b), depth + 1)
        except (Unresolved, Param):
            right = ["{*}"]          # `"/people/" + key.addingPercentEncoding(…)`
        return [l + r for l in left for r in right]
    if t.startswith('"') and t.endswith('"') and s.count('"') == 2:
        return _interpolate(sw, e, depth)
    if _IDENT.match(t):
        decl, typ, fn = _local_decl(sw, e, t)
        if decl is not None:
            return resolve_string(sw, decl, depth + 1)
        bound = _param(sw, e, t)
        if bound is not None:
            return resolve_string(sw, bound[0], depth + 1)
        return _const(sw, e.file, e.a, t, depth)
    m = re.match(r"^(?:self\.|Self\.|([A-Z]\w*(?:\.[A-Z]\w*)*)\.)(\w+)$", t)
    if m:
        return _const(sw, e.file, e.a, m.group(2), depth, m.group(1))
    m = re.match(r"^([a-z]\w*(?:\.\w+)*)\.(\w+)$", t)
    if m:
        owner = type_of(sw, Expr(e.file, e.a, e.a + len(m.group(1))))
        if m.group(2) == "path" and (owner or "").split(".")[-1] in _PATH_CARRIERS:
            raise Carried(t)
        if owner and not owner.startswith(("(", "[")):
            return _const(sw, e.file, e.a, m.group(2), depth, owner)
    m = re.match(r"^(?:(?:Self|self|[A-Z]\w*)\.)?([a-z]\w*)\s*\(", t)
    if m and s.endswith(")"):
        return _func_string(sw, e, m.group(1), depth)
    raise Unresolved(t)


def _top_char(s, ch, start=0):
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ch and depth == 0:
            if ch == "?" and (j + 1 < len(s) and s[j + 1] in ".?"):
                continue
            if ch == ":" and s[j - 1:j] == ":" :
                continue
            return j
    return -1


def _interpolate(sw, e, depth):
    """Every path a string literal can be. An interpolation of a path (or of
    one of several: `\\(base)/\\(id)/decide` where `base` is one of two
    routes) is spelled out; anything else is `{*}`."""
    t = e.text
    outs, j = [""], 1
    while j < len(t) - 1:
        if t.startswith("\\(", j):
            close = match_close(e.file.masked, e.a + j + 1)
            inner = Expr(e.file, e.a + j + 2, close)
            pieces = ["{*}"]
            try:
                vals = resolve_string(sw, inner, depth + 1)
                if vals and all(v.startswith("/") for v in vals):
                    pieces = list(dict.fromkeys(vals))
            except (Unresolved, Param):
                pass
            outs = [o + p for o in outs for p in pieces]
            j = close - e.a + 1
            continue
        outs = [o + t[j] for o in outs]
        j += 1
    return outs


def _const(sw, f, off, name, depth, owner=None):
    """A string constant: `static let name = "…"`, `let name = "…"` or
    `var name: String { "…" }`, in the owning type, the file, then anywhere."""
    hits = sw.string_decls().get(name, [])
    here = sw.enclosing_type(f, off)
    for x, m in sorted(hits, key=lambda h: h[0] is not f):
        t = sw.enclosing_type(x, m.start())
        if sw.enclosing_func(x, m.start()) is not None:
            continue          # a local of some other function
        if owner:
            if t is None or t.name != owner.split(".")[-1]:
                continue
        elif t is not None:
            if here is None or here.name != t.name:
                continue      # a bare name is the enclosing type's own
        if m.group(2) == "=":
            start = m.end()
            return resolve_string(sw, Expr(x, start, _stmt_end(x.skel, start)), depth + 1)
        close = match_close(x.skel, m.end() - 1)
        return _returns(sw, x, m.end(), close, depth)
    raise Unresolved(name)


def _returns(sw, x, a, b, depth):
    """Every path a computed body can return: each `return` (or the single
    expression), skipping the ones that are not readable — a server-named
    route returned ahead of the literal fallback."""
    body = x.skel[a:b]
    rets = list(re.finditer(r"\breturn\s+", body))
    if not rets:
        return resolve_string(sw, Expr(x, a, b), depth + 1)
    out = []
    for r in rets:
        start = a + r.end()
        try:
            out += resolve_string(sw, Expr(x, start, _stmt_end(x.skel, start)), depth + 1)
        except (Unresolved, Param):
            pass
    if not out:
        raise Unresolved(x.text(a, b))
    return out


def _func_string(sw, e, name, depth):
    """A function returning a path: its last statement's string, with its
    own parameters standing for `{*}`."""
    for fn in sw.funcs_by_name.get(name, []):
        if not re.search(r"->\s*String\s*$", fn.file.masked[fn.start:fn.body_a]):
            continue
        body = fn.file.skel[fn.body_a + 1:fn.body_b]
        stmts = [m for m in re.finditer(r"\breturn\s+", body)]
        start = fn.body_a + 1 + (stmts[-1].end() if stmts else len(body) - len(body.lstrip()))
        _ENV[id(fn)] = {p[1]: (None, p[2]) for p in fn.params}
        try:
            return resolve_string(sw, Expr(fn.file, start, _stmt_end(fn.file.skel, start)), depth + 1)
        except Unresolved:
            continue
        finally:
            _ENV.pop(id(fn), None)
    raise Unresolved(e.text)


# ── body shapes ──────────────────────────────────────────────────────────────

class Shape:
    """What a body encodes: `keys` (top-level JSON keys), or `opaque` with
    the reason it cannot be read from source (a server-provided body passed
    through as JSON)."""

    def __init__(self, keys=(), opaque=None, origin=""):
        self.keys, self.opaque, self.origin = set(keys), opaque, origin


_PASSTHROUGH_TYPE = re.compile(r"AnyCodableValue|JSONValue|AnyCodable\b|Data\b")


def resolve_body(sw, e, depth=0, hint=None):
    if depth > 8:
        raise Unresolved(e.text)
    e = _strip(e)
    t, s = e.text, e.skel
    if t in ("nil", ""):
        return Shape(origin="no body")
    if re.match(r"^\[[^\]]*\]\s*\(\s*\)$", s):
        return Shape(origin="dictionary")      # keys come from subscript writes
    # `.init(…)` / `.make(…)`: an implicit member of the declared type
    m = re.match(r"^\.(init|[a-z]\w*)\s*\(", s)
    if m and hint:
        typ = hint.strip().rstrip("?!")
        if m.group(1) == "init":
            return _struct_shape(sw, e.file, e.a, typ)
        t2 = sw.find_type(typ, e.file, e.a)
        return _func_shape(sw, e, m.group(1), depth, owner=t2)
    # optional map / closures that build a body: the type they construct
    m = re.match(r"^[\w.]+\??\.(?:map|flatMap)\s*\{", s)
    if m:
        inner = re.search(r"\b([A-Z][\w.]*)\s*\(", t[m.end():])
        if inner:
            return _struct_shape(sw, e.file, e.a, inner.group(1))
    q = _top_char(s, "?")
    if q > 0 and s[q - 1] == " ":
        colon = _top_char(s, ":", q)
        if colon > 0:
            a = resolve_body(sw, Expr(e.file, e.a + q + 1, e.a + colon), depth + 1)
            b = resolve_body(sw, Expr(e.file, e.a + colon + 1, e.b), depth + 1)
            if a.opaque or b.opaque:
                return a if a.opaque else b
            return Shape(a.keys | b.keys, origin=a.origin + " | " + b.origin)
    nc = s.find("??")
    if nc > 0 and _top_char(s, "?") == nc:
        return resolve_body(sw, Expr(e.file, e.a, e.a + nc), depth + 1)
    if s.startswith("[") and match_close(e.file.skel, e.a) == e.b - 1:
        return Shape(_dict_keys(e), origin="dictionary literal")
    m = re.match(r"^(?:JSONEncoder(?:\(\)|\.cavnar)\s*\.\s*encode)\s*\(", s)
    if m:
        close = match_close(e.file.skel, e.a + m.end() - 1)
        return resolve_body(sw, Expr(e.file, e.a + m.end(), close), depth + 1)
    m = re.match(r"^(AnyEncodable|AnyCodable)\s*\(", s)
    if m:
        close = match_close(e.file.skel, e.a + m.end() - 1)
        return resolve_body(sw, Expr(e.file, e.a + m.end(), close), depth + 1)
    if _IDENT.match(t):
        decl, typ, fn = _local_decl(sw, e, t)
        shape = None
        if decl is not None:
            shape = resolve_body(sw, decl, depth + 1, hint=typ)
        elif typ:
            shape = _typed_shape(sw, e, typ)
        if shape is None:
            bound = _param(sw, e, t)
            if bound is not None:
                try:
                    shape = resolve_body(sw, bound[0], depth + 1, hint=bound[1])
                except Unresolved:
                    # `{ await model.saveDoc($0) }`: the argument is a closure
                    # parameter; the wrapper's declared type is the body.
                    typ = _clean_type(bound[1])
                    if not typ or typ in ("Encodable", "Encodable & Sendable") or typ.startswith("("):
                        raise
                    shape = _typed_shape(sw, e, typ)
            else:
                prop = type_of(sw, e)
                if prop is None:
                    raise Unresolved(t)
                shape = _typed_shape(sw, e, prop)
        if not shape.opaque:
            shape.keys |= _subscript_writes(sw, e, t)
        return shape
    # `Type(…)` / `Type.Inner(…)` / `Type.init(…)`
    m = re.match(r"^((?:[A-Z]\w*\.)*[A-Z]\w*)(?:\.init)?\s*(?:<[^>]*>)?\s*\(", s)
    if m and s.endswith(")"):
        return _struct_shape(sw, e.file, e.a, m.group(1))
    # `Type.make(…)` / `make(…)`: a function returning a body
    m = re.match(r"^([a-z]\w*(?:\.\w+)*)\.([a-z]\w*)\s*\(", s)
    if m and s.endswith(")") and m.group(1) not in ("self",):
        owner_t = type_of(sw, Expr(e.file, e.a, e.a + len(m.group(1))))
        owner = sw.find_type(owner_t, e.file, e.a) if owner_t else None
        if owner is not None:
            return _func_shape(sw, e, m.group(2), depth, owner=owner)
    m = re.match(r"^(?:((?:[A-Z]\w*\.)*[A-Z]\w*|self|Self)\.)?([a-z]\w*)\s*\(", s)
    if m and s.endswith(")"):
        owner = None
        if m.group(1) and m.group(1) not in ("self", "Self"):
            owner = sw.find_type(m.group(1), e.file, e.a)
        elif m.group(1):
            owner = sw.enclosing_type(e.file, e.a)
        return _func_shape(sw, e, m.group(2), depth, owner=owner)
    # `x.body`, `proposal.postedBody`: a model property
    m = re.match(r"^[\w.?!]*\.(\w+)$", t)
    if m:
        prop = type_of(sw, e) or _member_type(sw, m.group(1))
        if prop is not None:
            return _typed_shape(sw, e, prop)
    raise Unresolved(t)


def _typed_shape(sw, e, typ):
    typ = typ.strip().rstrip("?!")
    if _PASSTHROUGH_TYPE.search(typ) and not typ.startswith("[String: AnyCodableValue]"):
        return Shape(opaque=f"{typ} passed through", origin=typ)
    if typ.startswith("["):
        # a dictionary handed in from elsewhere: its keys are not here
        raise Unresolved(f"{typ} built by the caller")
    if typ.startswith("(any") or typ in ("any Encodable", "Encodable"):
        raise Unresolved(typ)
    return _struct_shape(sw, e.file, e.a, typ)


def _member_type(sw, name):
    """The declared type of a property `name` when every declaration of
    that name in the app agrees on one — the fallback when the owner of
    `x.name` cannot be typed."""
    pat = re.compile(r"\b(?:let|var)\s+" + re.escape(name) + r"\s*:\s*([^=\n{]+)")
    types_ = set()
    for x in sw.files:
        for m in pat.finditer(x.skel):
            types_.add(x.masked[m.start(1):m.end(1)].strip())
    return types_.pop() if len(types_) == 1 else None


def _prop_type(sw, t, name):
    """The declared type of property `name` of type `t` (or an extension)."""
    pat = re.compile(r"\b(?:let|var)\s+" + re.escape(name) + r"\s*:\s*([^=\n{]+)")
    for part in sw.type_parts(t):
        m = pat.search(_top_level(part.file, part.a, part.b))
        if m:
            return m.group(1).strip()
    return None


def _clean_type(typ):
    typ = (typ or "").strip()
    typ = re.sub(r"^(?:some|any)\s+", "", typ)
    return typ.rstrip("?!").strip() or None


def type_of(sw, e, depth=0):
    """The declared type of a simple expression — `x`, `self.x`,
    `snap.winback`, `Type(…)` — or None."""
    if depth > 6:
        return None
    e = _strip(e)
    t = e.text
    m = re.match(r"^((?:[A-Z]\w*\.)*[A-Z]\w*)(?:\.init)?\s*\(", t)
    if m and t.endswith(")"):
        return m.group(1)
    chain = re.sub(r"[?!]", "", t)
    if not re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", chain):
        return None
    parts = chain.split(".")
    head, rest = parts[0], parts[1:]
    cur = None
    if head in ("self", "Self"):
        here = sw.enclosing_type(e.file, e.a)
        cur = here.qual if here else None
    elif head[0].isupper():
        cur = head
        while rest and rest[0][:1].isupper():
            cur += "." + rest.pop(0)
    else:
        head_e = Expr(e.file, e.a, e.a + len(head))
        decl, typ, fn = _local_decl(sw, head_e, head)
        if typ:
            cur = _clean_type(typ)
        elif decl is not None:
            cur = type_of(sw, decl, depth + 1)
        else:
            fm = None
            fn = sw.enclosing_func(e.file, e.a)
            if fn is not None:
                pat = re.compile(r"\bfor\s+(?:case\s+)?(?:let\s+|var\s+)?" + re.escape(head) + r"\s+in\s+")
                for fm in pat.finditer(e.file.skel, fn.body_a, e.a):
                    pass
            if fm is not None:
                it = type_of(sw, Expr(e.file, fm.end(), _stmt_end(e.file.skel, fm.end(), True)), depth + 1)
                cur = it[1:-1].strip() if it and it.startswith("[") and ":" not in it else None
            else:
                try:
                    bound = _param(sw, head_e, head)
                except (Param, Unresolved):
                    bound = None
                    f2 = sw.enclosing_func(e.file, e.a)
                    while f2 is not None and f2.param(head)[1] is None:
                        f2 = sw.enclosing_func(e.file, f2.start)
                    if f2 is not None:
                        cur = _clean_type(f2.param(head)[1][2])
                if bound is not None:
                    cur = _clean_type(bound[1])
                if cur is None and bound is None:
                    here = sw.enclosing_type(e.file, e.a)
                    if here is not None:
                        cur = _clean_type(_prop_type(sw, here, head))
                        if cur is None:
                            dm = re.search(r"\b(?:let|var)\s+" + re.escape(head) + r"\s*=\s*([A-Z][\w.]*)\s*\(",
                                           _top_level(here.file, here.a, here.b))
                            cur = dm.group(1) if dm else None
    for name in rest:
        if cur is None:
            return None
        if cur.startswith("("):
            tm = re.search(r"\b" + re.escape(name) + r"\s*:\s*([\w.\[\]: ]+?)\s*[,)]", cur)
            cur = _clean_type(tm.group(1)) if tm else None
            continue
        tt = sw.find_type(cur, e.file, e.a)
        if tt is None:
            return None
        cur = _clean_type(_prop_type(sw, tt, name))
    return cur


def _struct_shape(sw, f, off, name):
    t = sw.find_type(name, f, off)
    if t is None:
        raise Unresolved(f"type {name}")
    keys, note = type_keys(sw, t)
    if note:
        return Shape(opaque=note, origin=t.qual)
    return Shape(keys, origin=t.qual)


def _dict_keys(e):
    keys = set()
    skel = e.file.skel
    for a, b in split_top(skel, e.a + 1, e.b - 1):
        item = Expr(e.file, a, b)
        colon = _top_char(item.skel, ":")
        if colon < 0:
            continue
        k = item.text[:colon].strip()
        if k.startswith('"') and k.endswith('"'):
            keys.add(k[1:-1])
        else:
            keys.add("{dynamic}")
    return keys


def _subscript_writes(sw, e, name):
    fn = sw.enclosing_func(e.file, e.a)
    lo, hi = (fn.body_a, fn.body_b) if fn else (0, e.a)
    text = e.file.masked[lo:hi]
    keys = set(re.findall(r"\b" + re.escape(name) + r'\s*\[\s*"(\w+)"\s*\]\s*=(?!=)', text))
    if re.search(r"\b" + re.escape(name) + r"\s*\[\s*[^\"\]\s][^\]]*\]\s*=(?!=)", text) \
            or re.search(r"\b" + re.escape(name) + r"\.merge\(", text):
        keys.add("{dynamic}")
    return keys


def _func_shape(sw, e, name, depth, owner=None):
    cands = sw.funcs_by_name.get(name, [])
    if owner is not None:
        parts = sw.type_parts(owner)
        cands = [fn for fn in cands if any(p.file is fn.file and p.a < fn.start < p.b for p in parts)] or cands
    same = [fn for fn in cands if fn.file is e.file]
    for fn in (same or cands):
        sig = fn.file.masked[fn.start:fn.body_a]
        rm = re.search(r"->\s*([^{]+?)\s*$", sig)
        if not rm:
            continue
        ret = rm.group(1).strip()
        if ret in ("Self", "Self?"):
            ret = sw.enclosing_type(fn.file, fn.start).qual
        if not ret.startswith("["):
            return _typed_shape(sw, Expr(fn.file, fn.body_a, fn.body_a), ret)
        body = fn.file.masked[fn.body_a + 1:fn.body_b]
        rets = list(re.finditer(r"\breturn\s+", body))
        if not rets and body.strip():
            rets = []
            start = fn.body_a + 1 + (len(body) - len(body.lstrip()))
            return resolve_body(sw, Expr(fn.file, start, fn.body_b), depth + 1)
        shape = Shape(origin=f"{name}()")
        for r in rets:
            start = fn.body_a + 1 + r.end()
            sub = resolve_body(sw, Expr(fn.file, start, _stmt_end(fn.file.skel, start)), depth + 1)
            if sub.opaque:
                return sub
            shape.keys |= sub.keys
        return shape
    raise Unresolved(e.text)


def resolve_method(sw, e):
    e = _strip(e)
    t = re.sub(r"\.rawValue$", "", e.text)
    m = re.match(r'^(?:[\w.]*\.)?(post|put|patch|delete|get|head)$', t, re.I) or re.match(r'^"(\w+)"$', t)
    if m:
        return m.group(1).upper()
    if _IDENT.match(t):
        decl, typ, fn = _local_decl(sw, e, t)
        if decl is not None:
            return resolve_method(sw, decl)
        bound = _param(sw, e, t)
        if bound is not None:
            return resolve_method(sw, bound[0])
    raise Unresolved(t)


# ── writes ───────────────────────────────────────────────────────────────────

class Write:
    def __init__(self, site, paths, method, shape, chain, query=()):
        self.site, self.paths, self.method, self.shape, self.chain = site, paths, method, shape, chain
        self.query = set(query)

    @property
    def where(self):
        return f"{self.site.file.rel}:{self.site.file.line(self.site.a)}"


def _bindings(call, fn):
    """Every parameter of `fn` bound to `call`'s argument, or its default."""
    out, pos = {}, [e for lab, e in call.args if lab is None]
    i = 0
    for label, internal, typ, default in fn.params:
        if label is None:
            e = pos[i] if i < len(pos) else None
            i += 1
        else:
            e = call.arg(label)
        out[internal] = (e if e is not None else default, typ)
    return out


def _compatible(call, fn):
    labels = {p[0] for p in fn.params if p[0]}
    unlabeled = sum(1 for p in fn.params if p[0] is None)
    if any(lab and lab not in labels for lab, _ in call.args):
        return False
    if sum(1 for lab, _ in call.args if lab is None) > unlabeled:
        return False
    required = {p[0] for p in fn.params if p[0] and p[3] is None and "->" not in p[2]}
    return required <= {lab for lab, _ in call.args if lab}


def collect_writes(sw):
    """(writes, unresolved): every write call followed through wrappers to
    the call that names its path; unresolved holds what could not be read."""
    writes, unresolved, seen, done = [], [], set(), set()

    def visit(root, exprs, env, chain):
        # env: the wrappers bound so far, outermost caller last
        key = (root.file.rel, root.a, tuple((c.file.rel, c.a) for c in chain))
        if key in seen or len(env) > 10:
            return
        seen.add(key)
        _ENV.clear()
        for fn, b in env:
            _ENV[id(fn)] = b
        try:
            method = resolve_method(sw, exprs["method"]) if exprs["method"] is not None else "GET"
            if method not in WRITE_METHODS:
                return
            paths = resolve_string(sw, exprs["path"])
            try:
                shape = resolve_body(sw, exprs["body"]) if exprs["body"] is not None else Shape(origin="no body")
            except Unresolved as u:
                # The route is known and the body is not: the route is left
                # out of the key check, and the site must be allowlisted.
                site = chain[-1] if chain else root
                unresolved.append((site, [root] + chain, f"body: {u}"))
                shape = Shape(opaque=f"unreadable body: {u}", origin="unreadable")
            query = set()
            if exprs.get("query") is not None:
                try:
                    q = resolve_body(sw, exprs["query"])
                    query = set() if q.opaque else q.keys - {"{dynamic}"}
                except (Unresolved, Param):
                    pass
        except Carried:
            return              # read where the action is built (_carrier_writes)
        except Param as p:
            fn = p.func
            files = [fn.file] if fn.private else sw.files
            callers = [c for c in _calls_named(sw, fn.name, files)
                       if _compatible(c, fn) and sw.enclosing_func(c.file, c.a) is not fn
                       and not (c.file is root.file and c.a == root.a) and _receiver_ok(sw, c, fn)]
            for c in callers:
                visit(root, exprs, env + [(fn, _bindings(c, fn))], chain + [c])
            if not callers and not fn.file.path.endswith("APIClient.swift"):
                unresolved.append((chain[-1] if chain else root, [root] + chain, f"wrapper {fn.name}() has no callers"))
            return
        except Unresolved as u:
            site = chain[-1] if chain else root
            unresolved.append((site, [root] + chain, str(u)))
            return
        finally:
            _ENV.clear()
        site = chain[-1] if chain else root
        sig = (site.file.rel, site.a, tuple(paths), method, tuple(sorted(shape.keys)), shape.opaque, tuple(sorted(query)))
        if sig not in done:
            done.add(sig)
            writes.append(Write(site, paths, method, shape, chain, query))

    for call in seed_calls(sw):
        if os.sep + "CavnarAITests" + os.sep in call.file.path:
            continue
        path = call.arg("path", 0)
        if path is None:
            continue
        body = call.arg("body")
        if body is None:
            body = call.arg("bodyJSON")
        if body is None:
            body = _following_http_body(sw, call)
        visit(call, {"path": path, "method": call.arg("method"), "body": body, "query": call.arg("query")}, [], [])
    cw, cu = _carrier_writes(sw)
    return writes + cw, unresolved + cu


# ── lock-screen actions: a route and its body carried as a value ────────────
#
# PushManager's notification buttons are built as values —
# `BackgroundAction(path: "…", decision:/payload:/body:/savesDraft:/
# asksCoverForIssue: …)` — and one function, `perform`, posts whichever the
# value carries: `action.path` with its `payload`, else its `body` (an
# ActionBody case), else `{decision}` (DecisionBody), else `{}` (EmptyBody);
# a typed reply's `savesDraft` (SavedDraft) first posts `{draft}`
# (SaveDraftBody) to its own path; "Ask someone to cover"
# (`asksCoverForIssue`) posts `{name}` (CoverBody). Each value's literal path
# and body are read where it is built; the send sites that only pass a
# carried path on (`action.path`, `save.path`) are not writes of their own.
# test_lock_screen_actions_are_read_where_they_are_built pins `perform`'s
# order, so a new branch there cannot slip past this reading.

_PATH_CARRIERS = {"BackgroundAction", "SavedDraft"}


def _present(e):
    return e is not None and e.text.strip() not in ("", "nil")


def _action_case_shape(sw, f, off, case):
    """The keys an ActionBody case encodes: its section of the enum's
    `encode(to:)` (`forKey: .k` through the enum's CodingKeys), or — a case
    that hands the encoder to its associated value — that value's type."""
    t = sw.find_type("ActionBody", f, off)
    if t is None:
        raise Unresolved("type ActionBody")
    enc = next((fn for fn in t.file.funcs if fn.name == "encode" and t.a < fn.start < t.b), None)
    if enc is None:
        raise Unresolved("ActionBody.encode(to:)")
    body = t.file.masked[enc.body_a:enc.body_b]
    cases = list(re.finditer(r"\bcase\s+\.(\w+)\b", body))
    for i, cm in enumerate(cases):
        if cm.group(1) != case:
            continue
        sec = body[cm.end():cases[i + 1].start() if i + 1 < len(cases) else len(body)]
        ck = _coding_keys(sw, t)
        keys = {ck[k] for k in re.findall(r"forKey:\s*\.(\w+)", sec) if k in ck}
        if re.search(r"\.encode\(to:\s*encoder\)", sec):
            dm = re.search(r"\bcase\s+" + re.escape(case) + r"\s*\(\s*(?:\w+\s*:\s*)?([\w.]+)",
                           _top_level(t.file, t.a, t.b))
            if not dm:
                raise Unresolved(f"ActionBody.{case}'s value")
            keys |= _struct_shape(sw, f, off, dm.group(1)).keys
        return Shape(keys, origin=f"ActionBody.{case}")
    raise Unresolved(f"ActionBody.{case}")


def _carrier_body(sw, e):
    """A carried body: an ActionBody case (`.approve(expectedDraft: x)`), a
    body built from a dictionary literal (`PushActionBody(["k": …])`), or
    anything resolve_body reads."""
    e = _strip(e)
    m = re.match(r"^\.(\w+)\b", e.skel)
    if m:
        return _action_case_shape(sw, e.file, e.a, m.group(1))
    m = re.match(r"^[A-Z][\w.]*\s*\(\s*\[", e.skel)
    if m:
        lb = e.a + m.end() - 1
        return Shape(_dict_keys(Expr(e.file, lb, match_close(e.file.skel, lb) + 1)), origin="dictionary literal")
    return resolve_body(sw, e)


def _later_sets(sw, c, labels):
    """`var action = BackgroundAction(…)` then `action.body = …`: the
    expressions set on the value after it is built, in the same function."""
    f = c.file
    line_start = f.skel.rfind("\n", 0, c.a) + 1
    vm = re.search(r"\bvar\s+(\w+)\s*=\s*$", f.skel[line_start:c.a])
    if not vm:
        return []
    fn = sw.enclosing_func(f, c.a)
    hi = fn.body_b if fn else len(f.skel)
    out = []
    pat = re.compile(r"\b" + re.escape(vm.group(1)) + r"\.(" + "|".join(labels) + r")\s*=(?!=)\s*")
    for m in pat.finditer(f.skel, c.b, hi):
        out.append(Expr(f, m.end(), _stmt_end(f.skel, m.end())))
    return out


def _carrier_writes(sw):
    writes, unresolved = [], []
    _ENV.clear()
    for c in _calls_named(sw, "BackgroundAction", sw.files):
        if c.arg("path") is None:
            continue
        try:
            paths = resolve_string(sw, c.arg("path"))
            if _present(c.arg("asksCoverForIssue")):
                shape = _struct_shape(sw, c.file, c.a, "CoverBody")
            else:
                parts = [_carrier_body(sw, e) for e in [c.arg("payload"), c.arg("body")] if _present(e)]
                parts += [_carrier_body(sw, e) for e in _later_sets(sw, c, ("payload", "body"))]
                if not parts:
                    parts = [_struct_shape(sw, c.file, c.a, "DecisionBody") if _present(c.arg("decision"))
                             else Shape(origin="EmptyBody")]
                shape = Shape(set().union(*(p.keys for p in parts)),
                              opaque=next((p.opaque for p in parts if p.opaque), None),
                              origin=" | ".join(p.origin for p in parts))
        except (Unresolved, Param) as u:
            unresolved.append((c, [c], f"carried action: {u}"))
            continue
        writes.append(Write(c, paths, "POST", shape, []))
    for c in _calls_named(sw, "SavedDraft", sw.files):
        if c.arg("path") is None:
            continue
        try:
            writes.append(Write(c, resolve_string(sw, c.arg("path")), "POST",
                                _struct_shape(sw, c.file, c.a, "SaveDraftBody"), []))
        except (Unresolved, Param) as u:
            unresolved.append((c, [c], f"carried draft: {u}"))
    return writes, unresolved


def _following_http_body(sw, call):
    """`var r = makeRequest(path, method: "POST"); r.httpBody = try
    JSONEncoder().encode(body)` — the body set on the request just after."""
    f = call.file
    fn = sw.enclosing_func(f, call.a)
    hi = fn.body_b if fn else min(len(f.skel), call.b + 400)
    m = re.compile(r"\.httpBody\s*=\s*").search(f.skel, call.b, hi)
    if not m:
        return None
    return Expr(f, m.end(), _stmt_end(f.skel, m.end()))


def load_swift():
    files = []
    for dirpath, dirnames, filenames in os.walk(IOS):
        dirnames[:] = [d for d in dirnames if d not in ("CavnarAITests", "build", "DerivedData")
                       and not d.endswith(".xcodeproj")]
        for name in filenames:
            if name.endswith(".swift"):
                p = os.path.join(dirpath, name)
                with open(p, encoding="utf-8") as fh:
                    files.append(SwiftFile(p, fh.read()))
    return Swift(files)


# ═════════════════════════════════════════════════════════════════════════════
# Matching a Swift path to a Flask rule
# ═════════════════════════════════════════════════════════════════════════════

def _rule_regex(rule):
    out = "^"
    for part in re.split(r"(<[^>]+>)", rule):
        if part.startswith("<"):
            conv = part[1:-1].split(":")[0] if ":" in part else "string"
            out += {"int": r"(?:\d+|\{\*\})", "path": r".+"}.get(conv, r"[^/]+")
        else:
            out += re.escape(part)
    return re.compile(out + "$")


def match_rules(path, method, rules):
    """The rules a Swift path template can reach for `method`. An
    interpolated segment matches a converter first; only when no rule has
    one there does it stand for the literal segments it could be."""
    path = path.split("?")[0]
    exact = [r for r in rules if method in r["methods"] and r["_re"].match(path)]
    if exact:
        # Flask's own order: a literal segment wins over a converter
        # (/people/merge, not /people/<key>).
        fewest = min(r["rule"].count("<") for r in exact)
        return [r for r in exact if r["rule"].count("<") == fewest]
    loose = re.compile("^" + re.escape(path).replace(re.escape("{*}"), r"[^/]+") + "$")
    return [r for r in rules if method in r["methods"] and loose.match(re.sub(r"<[^>]+>", "1", r["rule"]))]


# ═════════════════════════════════════════════════════════════════════════════
# The allowlists — keep them small and honest
# ═════════════════════════════════════════════════════════════════════════════

# (rule, key) -> why no iOS caller of that route sends it. Every entry was
# checked against the route's code and the web's own body on 10/7/26: none is
# a key the server needs from the phone. Add one only with that check, and
# never to make a real omission pass — fix the Swift body instead.

# Optional, with the server's default, and the web does not send it either.
_WEB_TOO = "optional (server default); the web's body leaves it out too"
# The free-text note beside a reason code: the web's reason picker has an
# optional note box, the phone's (RecReasonDialog) is the codes alone.
_NOTE = "the optional note beside a reason code; the phone's reason picker is codes only (RecReasonDialog)"
# A setting with a web control and no iOS one: a capability gap the parity
# audit tracks, not a body that drops a key the phone has.
_WEB_ONLY = "a web-only control (no iOS screen edits it) — a capability gap, not a dropped key"
# Read by the shared login throttle (auth_routes._request_device_token, via
# _throttle_context) on every auth route; only /login carries the device the
# phone remembers (LoginRequestBody.deviceToken).
_THROTTLE = "the login throttle's remembered-device token, read on every auth route; the phone sends it on /login"

NOT_SENT_BY_IOS = {
    # optional, and the web leaves it out too
    ("/mobile/api/account/alert-settings", "alert_max_per_day"): _WEB_TOO + " (the web stopped sending it; kept as stored)",
    ("/mobile/api/account/alert-settings", "expected_version"): _WEB_TOO + " (models' optional row_version check)",
    ("/mobile/api/account/update-profile", "expected_version"): _WEB_TOO + " (models' optional row_version check)",
    ("/mobile/api/account/update-profile", "category"): _WEB_TOO + " (the intelligence cohort, set by admin)",
    ("/mobile/api/account/staff/<int:membership_id>", "role"): _WEB_TOO + " (moves a login between tiers; name, job and active are what both send)",
    ("/mobile/api/account/memory/add", "scope"): _WEB_TOO + " (scope is its own route, /account/memory/scope)",
    ("/mobile/api/account/memory/restore", "due_on"): _WEB_TOO,
    ("/mobile/api/account/memory/restore", "valid_until"): _WEB_TOO,
    ("/mobile/api/actions/<int:action_id>/cancel", "reason"): _WEB_TOO + " (the why is asked afterwards, on the undo-why route)",
    ("/mobile/api/actions/<int:action_id>/cancel", "reason_code"): _WEB_TOO + " (the why is asked afterwards, on the undo-why route)",
    ("/mobile/api/actions/snooze", "days"): _WEB_TOO + " (action_queue.SNOOZE_DAYS)",
    ("/mobile/api/home/dismiss", "days"): _WEB_TOO,
    ("/mobile/api/recs/event", "days"): _WEB_TOO + " (a snooze's length)",
    ("/mobile/api/recs/event", "metric"): _WEB_TOO + " (a tracker's metric comes from the recommendation)",
    ("/mobile/api/recs/checkin", "note"): _WEB_TOO,
    ("/mobile/api/outcomes", "module"): _WEB_TOO,
    ("/mobile/api/outcomes", "surface"): _WEB_TOO + " ('home')",
    ("/mobile/api/outcomes", "window_days"): _WEB_TOO,
    ("/mobile/api/issues/<int:issue_id>/resolve", "note"): _WEB_TOO,
    # (/mobile/api/issues/routing: the phone no longer writes issue routing —
    # the restaurant's alert rules are set on the web since the iOS
    # readability round, 10/8/26.)
    ("/mobile/api/food-cost/invoices/<int:import_id>/apply", "use_checked"): _WEB_TOO + " (Ask's apply_invoice_lines)",
    ("/mobile/api/food-cost/recipe-drafts/<int:draft_id>/accept", "lines"): _WEB_TOO + " (the draft's own lines)",
    ("/mobile/api/labor/labor-standards", "all"): _WEB_TOO + " (both send lunch and dinner)",
    ("/mobile/api/labor/auto-draft", "external_tool"): _WEB_TOO + " (set at onboarding)",
    ("/mobile/api/labor/covers", "csv"): _WEB_TOO + " (both send rows)",
    ("/mobile/api/labor/covers", "from_pos"): _WEB_TOO + " (the POS count is filled nightly, covers.sync_from_pos)",
    ("/mobile/api/labor/demand-signals", "source"): _WEB_TOO + " ('manual')",
    ("/mobile/api/labor/schedule/adopt-admin-saves", "history_id"): _WEB_TOO + " (the latest week)",
    ("/mobile/api/labor/schedule/apply-fixes", "history_id"): _WEB_TOO + " (a ledger key only)",
    ("/mobile/api/labor/schedule/optimize", "hours_budget"): _WEB_TOO + " (both send daily_target_hours)",
    ("/mobile/api/labor/shift-requests/<int:request_id>/decide", "note"): _WEB_TOO,
    ("/mobile/api/labor/shift-requests/<int:request_id>/offer", "note"): _WEB_TOO,
    ("/mobile/api/labor/time-off/<int:request_id>/decide", "note"): _WEB_TOO,
    ("/mobile/api/labor/staff-contacts", "phone"): _WEB_TOO + " (both send employee_name and email)",
    ("/mobile/api/labor/staff-contacts", "pos_id"): _WEB_TOO,
    ("/mobile/api/labor/staff-settings", "desired_hours"): _WEB_TOO + " (the employee's own, /staff/api/preferences)",
    ("/mobile/api/labor/staff-settings", "preferred_dayparts"): _WEB_TOO + " (the employee's own, /staff/api/preferences)",
    ("/mobile/api/labor/rules", "closed_dates"): _WEB_TOO + " (each date saves on its own, /account/closures)",
    ("/mobile/api/people/identity/<int:question_id>", "keep"): _WEB_TOO + " (both send same)",
    ("/mobile/api/templates", "category"): _WEB_TOO + " ('general')",
    ("/mobile/api/task-sheets", "title"): _WEB_TOO + " (both create from job_code and shift_kind)",
    ("/mobile/api/task-sheets", "days_of_week"): _WEB_TOO + " (both create from job_code and shift_kind)",
    ("/mobile/api/task-sheets", "requires_signoff"): _WEB_TOO + " (both create from job_code and shift_kind)",
    ("/mobile/api/task-sheets/<int:sheet_id>", "title"): _WEB_TOO + " (a partial update: only keys sent change)",
    ("/mobile/api/account/hours", "closures"): "closed dates save one at a time on /account/closures (parity #7): "
                                                "a list on Save hours could drop a date",
    ("/mobile/api/account/hours", "closures_base"): "goes with `closures`, which the phone no longer sends on Save hours",
    # the reason picker's free-text note
    ("/mobile/api/guest-winback/<int:draft_id>/dismiss", "reason"): _NOTE,
    ("/mobile/api/labor/schedule/recommendation", "reason"): _NOTE,
    ("/mobile/api/recs/event", "reason"): _NOTE,
    # sent another way, or an older name for a key the phone sends
    ("/mobile/api/food-cost/invoices", "idempotency_key"): "a multipart upload: the key rides as the Idempotency-Key header (APIClient.upload)",
    ("/mobile/api/food-cost/recipes/scan", "idempotency_key"): "a multipart upload: the key rides as the Idempotency-Key header (APIClient.upload)",
    ("/staff/api/signup/claim", "signup_token"): "sent as the X-Signup-Token header, never in a body or URL",
    ("/staff/api/device-tokens/<apns_token>", "apns_token"): "the token is in the DELETE's path",
    ("/staff/api/device-tokens/<apns_token>", "token"): "the token is in the DELETE's path",
    ("/mobile/api/labor/rules", "closer_roles"): "set on its own route, /labor/closers, which the phone uses",
    ("/mobile/api/labor/rules", "role_close_stays"): "the older name of role_close_mins, which the phone sends",
    ("/mobile/api/guest-newsletter/draft", "topic"): "an alternative to prompt (`prompt or topic`), which the phone sends",
    ("/staff/api/availability", "unavailable_days"): "older apps' alternative to week, which the phone sends",
    ("/staff/api/tasks/complete", "task_date"): "older apps only: assignment_id names the day now (staff_routes._tick)",
    ("/mobile/api/forgot-password", "device_token"): _THROTTLE,
    ("/mobile/api/reset-password", "device_token"): _THROTTLE,
    ("/mobile/api/register", "device_token"): _THROTTLE,
    ("/mobile/api/verify-2fa", "device_token"): _THROTTLE,
}

# Write call sites whose path or body is not in the Swift source, by (file,
# enclosing function) — each a route or a body the server named.
UNREADABLE_OK = {
    ("RecMemoryViews.swift", "answerUndoWhy"): "the undo-why ask carries its own route (UndoAskWhy.route)",
    ("HomeFollowThrough.swift", "run"): "a follow-through step's route is the server's (step.route.mobile)",
    ("AskCavnarViewModel.swift", "confirm"): "a confirmed proposal posts the server's route and body (proposal.route.mobile)",
    ("StaffRequestsViews.swift", "commit"): "an undone withdraw/cancel: PendingUndo.path holds one of two literal "
                                            "/staff/api/time-off paths, body StaffEmptyBody",
    ("ReviewsListViewModel.swift", "bulkApprove"): "each chunk is a BulkApproveBody from bulkApproveBodies "
                                                   "(review_ids, limit, review_hashes) — pinned key by key in "
                                                   "ReviewsIntelParityTests.testBulkApproveSendsReviewIdsInChunksOfTwentyFive",
    ("PendingWriteQueue.swift", "drain"): "the offline queue replays the path, method and body it was given",
    ("PendingWriteQueue.swift", "enqueue"): "the offline queue replays the path, method and body it was given",
    ("TaskSheetsScreen.swift", "add"): "the line's fields are TSLineForm.save()'s [String: String] literal "
                                       "(every key the line routes read), handed through onSave",
    ("TaskSheetsScreen.swift", "save"): "the line's fields are TSLineForm.save()'s [String: String] literal "
                                        "(every key the line routes read), handed through onSave",
}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures and tests
# ═════════════════════════════════════════════════════════════════════════════

_SERVER = {}


def _start_server_probe():
    if "proc" not in _SERVER:
        out = tempfile.NamedTemporaryFile(prefix="bodyparity-", suffix=".json", delete=False).name
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
        _SERVER["out"] = out
        _SERVER["proc"] = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--server-reads", out],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _server_rules():
    _start_server_probe()
    if "rules" not in _SERVER:
        so, se = _SERVER["proc"].communicate(timeout=120)
        assert _SERVER["proc"].returncode == 0, se.decode()[-3000:]
        with open(_SERVER["out"]) as fh:
            rules = json.load(fh)
        os.unlink(_SERVER["out"])
        for r in rules:
            r["_re"] = _rule_regex(r["rule"])
        _SERVER["rules"] = rules
    return _SERVER["rules"]


@functools.lru_cache(maxsize=None)
def _swift():
    sw = load_swift()
    return sw, collect_writes(sw)


@functools.lru_cache(maxsize=None)
def _pairs():
    """(write, rule) for every iOS write and each route it reaches, and the
    writes whose path matches no route."""
    _start_server_probe()          # import the app while Swift is parsed
    sw, (writes, unresolved) = _swift()
    rules = _server_rules()
    pairs, orphans = [], []
    for w in writes:
        for p in w.paths:
            if not p.startswith(PREFIXES):
                continue
            hit = match_rules(p, w.method, rules)
            if not hit:
                orphans.append((w, p))
            for r in hit:
                pairs.append((w, r))
    return pairs, orphans, unresolved


def _missing():
    """{(rule, key): [where …]} — keys a route reads that no iOS caller of
    that route sends. The union over callers: a partial update (one sheet
    sends `active`, another `max_hours`) is several callers of one route."""
    pairs, _, _ = _pairs()
    by_rule = {}
    for w, r in pairs:
        by_rule.setdefault(r["rule"], (r, []))[1].append(w)
    out = {}
    for rule, (r, ws) in by_rule.items():
        if any(w.shape.opaque or "{dynamic}" in w.shape.keys for w in ws):
            continue          # a server-written body passed through
        sent = set().union(*(w.shape.keys for w in ws))
        # a key the phone puts in the query string, where the route reads it too
        sent |= set().union(*(w.query for w in ws)) & set(r["args_keys"])
        for group in r["aliases"]:
            if sent & set(group):
                sent |= set(group)
        for k in r["keys"]:
            if k not in sent:
                out[(rule, k)] = sorted({f"{w.where} ({w.shape.origin})" for w in ws})
    return out


def test_the_encoder_has_no_key_strategy():
    """The keys are the Swift names (or CodingKeys raw values) as written:
    APIClient's encoder sets no keyEncodingStrategy. A snake-case strategy
    would change every key this test reads."""
    with open(os.path.join(IOS, "CavnarAI", "Core", "APIClient.swift")) as fh:
        src = fh.read()
    assert "static let cavnar: JSONEncoder = JSONEncoder()" in src
    assert "keyEncodingStrategy" not in src


def test_the_parser_finds_the_writes():
    """Guards the guard: a parser that silently matched nothing would pass
    every other test here."""
    pairs, orphans, unresolved = _pairs()
    rules = {r["rule"] for _, r in pairs}
    assert len(rules) >= 200, len(rules)
    for known in ("/mobile/api/account/auto-approve", "/mobile/api/food-cost/waste",
                  "/staff/api/tasks/complete", "/mobile/api/recs/event"):
        assert any(r.startswith(known) for r in rules), known


def test_every_ios_write_reaches_a_route():
    """A path the phone writes to that no route serves is a 404 in a phone
    nobody is watching."""
    _, orphans, _ = _pairs()
    assert not orphans, "\n".join(f"{w.where}: {w.method} {p}" for w, p in orphans)


def _unreadable():
    """{(file, function): [where: why]} for every write the parser could
    not read, keyed by the outermost app function on the way in."""
    _, _, unresolved = _pairs()
    sw, _ = _swift()
    out = {}
    for call, calls, why in unresolved:
        here = next(((c, sw.enclosing_func(c.file, c.a)) for c in reversed(calls)
                     if sw.enclosing_func(c.file, c.a) is not None), (call, None))
        key = (os.path.basename(here[0].file.path), here[1].name if here[1] else "")
        out.setdefault(key, set()).add(f"{call.file.rel}:{call.file.line(call.a)}: {why}")
    return out


def test_every_ios_write_is_readable():
    """Every write's path and body resolved, or listed with the reason it
    is a route or body the server named."""
    left = {k: v for k, v in _unreadable().items() if k not in UNREADABLE_OK}
    assert not left, "\n".join(f"{k[0]} {k[1]}(): {'; '.join(sorted(v))}" for k, v in sorted(left.items()))


def test_the_unreadable_list_has_no_stale_entries():
    stale = [k for k in UNREADABLE_OK if k not in _unreadable()]
    assert not stale, stale


def test_the_server_reads_no_key_the_ios_body_omits():
    missing = _missing()
    left = {k: v for k, v in missing.items() if k not in NOT_SENT_BY_IOS}
    assert not left, "\n".join(f"{rule} reads {key!r}; not sent by {', '.join(sorted(set(ws))[:3])}"
                               for (rule, key), ws in sorted(left.items()))


def test_the_allowlist_has_no_stale_entries():
    missing = _missing()
    stale = [k for k in NOT_SENT_BY_IOS if k not in missing]
    assert not stale, stale


def test_ios_sends_no_misspelled_key():
    """A key the phone sends that the route never reads is a warning; one
    a single edit from a key the route does read and the phone does not
    send is a typo, and fails."""
    pairs, _, _ = _pairs()
    typos, unread = [], set()
    for w, r in pairs:
        if w.shape.opaque or not r["keys"] or r["dynamic"]:
            continue
        for k in w.shape.keys - set(r["keys"]) - {"{dynamic}"}:
            near = [s for s in r["keys"] if s not in w.shape.keys
                    and (s.replace("_", "").lower() == k.replace("_", "").lower()
                         or difflib.SequenceMatcher(None, s, k).ratio() >= 0.88)]
            if near:
                typos.append(f"{w.where}: sends {k!r}, {r['rule']} reads {near[0]!r}")
            else:
                unread.add((r["rule"], k))
    if unread:
        warnings.warn("iOS sends keys these routes never read: " +
                      "; ".join(f"{r} {k}" for r, k in sorted(unread)))
    assert not typos, "\n".join(typos)


# ── the parser itself ────────────────────────────────────────────────────────

def _sw(src, name="T.swift"):
    return Swift([SwiftFile(os.path.join(IOS, "X", name), src)])


def test_lexer_blanks_comments_and_strings_but_keeps_offsets():
    src = 'let a = "x(\\("y)"))" // send(\n/* method: .post */ f(a)'
    masked, skel = lex(src)
    assert len(masked) == len(skel) == len(src)
    assert "send(" not in masked and "method" not in masked
    assert skel.count("(") == skel.count(")") == 1


def test_parser_follows_a_wrapper_and_reads_coding_keys():
    sw = _sw('''
struct Body: Encodable {
    let includeFour: Bool
    let limit: Int
    var computed: Int { 1 }
    static let shared = 1
    enum CodingKeys: String, CodingKey { case includeFour = "include_4star", limit }
}
final class VM {
    private func post(_ path: String, _ body: some Encodable) async {
        _ = try? await client.send(path, method: .post, body: body)
    }
    func go(id: Int) async {
        await post("/mobile/api/reviews/\\(id)/approve", Body(includeFour: true, limit: 3))
        var extra: [String: AnyCodableValue] = ["a": .int(1)]
        extra["b"] = .bool(true)
        _ = try? await client.send("/mobile/api/x", method: .patch, body: extra)
    }
}
''')
    writes, unresolved = collect_writes(sw)
    got = {(w.method, tuple(w.paths)): w.shape.keys for w in writes}
    assert got[("POST", ("/mobile/api/reviews/{*}/approve",))] == {"include_4star", "limit"}
    assert got[("PATCH", ("/mobile/api/x",))] == {"a", "b"}
    assert not unresolved


def test_parser_reads_a_custom_encoder():
    sw = _sw('''
struct P: Encodable {
    let date: String
    var note: String?
    enum CodingKeys: String, CodingKey { case date, note, shiftStart = "shift_start" }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(date, forKey: .date)
        try c.encode("9", forKey: .shiftStart)
    }
}
func f() async { _ = try? await client.send("/mobile/api/p", method: .post, body: P(date: "", note: nil)) }
''')
    writes, _ = collect_writes(sw)
    assert writes[0].shape.keys == {"date", "shift_start"}


def test_server_reader_follows_the_body_into_helpers():
    import flask
    request = flask.request  # noqa: F841 — the name the reader looks for

    def helper(rid, payload):
        return payload.get("include_4star"), "limit" in payload

    def write(fn):
        b = request.get_json(silent=True) or {}
        return fn(1, b)

    def view():
        data = request.get_json(silent=True) or {}
        for k in ("a", "b"):
            data.get(k)
        name = data.get("employee_name") or data.get("name")
        return helper(1, data), data["count"], name, write(lambda rid, b: b.get("text"))

    keys, dynamic, _, aliases, _ = _reads(view)
    assert {"include_4star", "limit", "a", "b", "count", "employee_name", "name", "text"} <= set(keys)
    assert frozenset({"employee_name", "name"}) in aliases


def test_lock_screen_actions_are_read_where_they_are_built():
    """PushManager's buttons are read from their BackgroundAction values,
    not allowlisted as unreadable (re-audit 10/8/26, #8). `perform` still
    sends in the order _carrier_writes reads: payload, ActionBody, decision,
    empty — a typed reply's draft first, and a cover ask's name."""
    with open(os.path.join(IOS, "CavnarAI", "Push", "PushManager.swift")) as fh:
        src = fh.read()
    perform = src[src.index("nonisolated static func perform("):src.index("nonisolated static func scheduleSendAllowed")]
    order = [perform.index(x) for x in ("if let save = action.savesDraft", "body: SaveDraftBody(draft: save.text)",
                                         "if let payload = action.payload", "else if let body = action.body",
                                         "else if let decision = action.decision",
                                         "body: DecisionBody(decision: decision)", "body: EmptyBody()")]
    assert order == sorted(order)
    assert "action.path, method: .post, body: CoverBody(name: name)" in src
    w, unresolved = _carrier_writes(load_swift())
    assert not unresolved
    got = {}
    for x in w:
        for p in x.paths:
            got.setdefault(p, set()).update(x.shape.keys)
    assert got["/mobile/api/account/not-me"] == {"login_user_id"}
    assert got["/mobile/api/labor/time-off/{*}/decide"] == {"decision"}
    assert got["/mobile/api/labor/shift-requests/{*}/decide"] == {"decision"}
    # The words the push showed, and their fingerprint when they were clipped
    # (re-audit 10/8/26: draft_hash rides every review push).
    assert got["/mobile/api/reviews/{*}/approve"] == {"expected_draft", "expected_draft_hash"}
    assert got["/mobile/api/reviews/{*}/save-draft"] == {"draft"}
    assert got["/mobile/api/issues/{*}/ask-cover"] == {"name"}
    assert got["/mobile/api/staff-brief/approve"] == {"day", "text", "expected_rev"}   # the brief the push showed (re-audit 10/8/26)
    assert not any(k[0] == "PushManager.swift" for k in UNREADABLE_OK)


def test_parser_reads_a_carried_action():
    sw = _sw('''
struct BackgroundAction: Equatable {
    let path: String
    let decision: String?
    var body: ActionBody? = nil
}
enum ActionBody: Encodable {
    case notMe(loginUserId: Int)
    case rec(RecBody)
    private enum Keys: String, CodingKey { case loginUserId = "login_user_id" }
    func encode(to encoder: Encoder) throws {
        switch self {
        case .notMe(let id):
            var c = encoder.container(keyedBy: Keys.self)
            try c.encode(id, forKey: .loginUserId)
        case .rec(let body):
            try body.encode(to: encoder)
        }
    }
}
struct RecBody: Encodable { let key: String; let event: String }
struct DecisionBody: Encodable { let decision: String }
func make(_ kind: String, id: Int) -> BackgroundAction? {
    let base = kind == "t" ? "/mobile/api/time-off" : "/mobile/api/shifts"
    if kind == "n" { return BackgroundAction(path: "/mobile/api/not-me", decision: nil, body: .notMe(loginUserId: id)) }
    if kind == "r" {
        var a = BackgroundAction(path: "/mobile/api/recs", decision: nil)
        a.body = .rec(RecBody(key: "k", event: "e"))
        return a
    }
    return BackgroundAction(path: "\\(base)/\\(id)/decide", decision: "approve")
}
func perform(_ action: BackgroundAction) async {
    _ = try? await client.send(action.path, method: .post, body: DecisionBody(decision: "x"))
}
''')
    writes, unresolved = collect_writes(sw)
    got = {tuple(w.paths): w.shape.keys for w in writes}
    assert got[("/mobile/api/not-me",)] == {"login_user_id"}
    assert got[("/mobile/api/recs",)] == {"key", "event"}
    assert got[("/mobile/api/time-off/{*}/decide", "/mobile/api/shifts/{*}/decide")] == {"decision"}
    assert not unresolved


def test_rule_matching_prefers_a_converter():
    rules = [{"rule": r, "methods": ["POST"], "_re": _rule_regex(r)} for r in
             ("/mobile/api/connections/google", "/mobile/api/reviews/<int:rid>/approve")]
    assert [r["rule"] for r in match_rules("/mobile/api/reviews/{*}/approve", "POST", rules)] == \
        ["/mobile/api/reviews/<int:rid>/approve"]
    assert [r["rule"] for r in match_rules("/mobile/api/connections/{*}", "POST", rules)] == \
        ["/mobile/api/connections/google"]
    rules = [{"rule": r, "methods": ["POST"], "_re": _rule_regex(r)} for r in
             ("/mobile/api/people/<key>", "/mobile/api/people/merge")]
    assert [r["rule"] for r in match_rules("/mobile/api/people/merge", "POST", rules)] == \
        ["/mobile/api/people/merge"]


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--server-reads":
    sys.path.insert(0, ROOT)
    _server_main(sys.argv[2])
