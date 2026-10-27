"""Which keys of a phone payload the app's Swift models REQUIRE to be
non-null — read from the Swift sources themselves (blind re-audit 10/27/26,
B4/B6 "[P1] the installed-build response guard ignores nulls").

tests/test_ios_response_shapes.py compares each captured payload with its
v1 fixture. A null where the fixture held a value used to match anything
("every field the phone reads is optional" — true of the schedule models it
was written for, not of `MeResponse.user: User` or `HomeSummary.
restaurantName: String`). This answers, for a key path such as
`$.user.username` or `$.modules[].count`, whether some model that decodes
the route (ResponseShapeContractTests.swift's `Self.decode(T.self, "name")`
lines) would fail on a null there:

* a synthesized `Decodable`: a stored property whose type is not optional
  (`T?`, `T!`, `Optional<T>`), and not a property wrapper (`@LenientBool`);
  a `let` with an initial value is not decoded at all;
* a custom `init(from:)`: a key read with `try c.decode(…, forKey: .k)` (or a
  nested container) is required; `decodeIfPresent` tolerates a null at the
  key; `try?` or a helper (`c.setupText(.k)`) tolerates anything under it;
* a null array ELEMENT is required unless the element type is optional;
* nothing under a key read leniently (`try?`, a helper, a wrapper, a type
  whose own `init(from:)` reads a single value) is ever required.

A path that no model reads is not required. The parser is the body-parity
test's Swift index (tests/test_ios_request_body_parity.py). Where it cannot
tell, the answer is "required" (a test that says so is fixed with a
`NULLABLE` entry naming why, never by guessing optional).
"""
import functools
import os
import re

import test_ios_request_body_parity as bp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT_TEST = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAITests", "ResponseShapeContractTests.swift")

_LEAVES = {"String", "Int", "Double", "Float", "Bool", "Date", "Decimal", "URL", "Int64", "UInt", "CGFloat",
           "Data", "UUID"}
_DECODE_STRICT = re.compile(r"\btry\s+[A-Za-z_]\w*\.(?:decode|nestedContainer|nestedUnkeyedContainer)\(")


@functools.lru_cache(maxsize=None)
def swift():
    return bp.load_swift()


@functools.lru_cache(maxsize=None)
def root_types():
    """{fixture name: [Swift type names]} — what ResponseShapeContractTests
    decodes each captured fixture as."""
    with open(SWIFT_TEST, encoding="utf-8") as fh:
        src = fh.read()
    out = {}
    for m in re.finditer(r"Self\.decode\(\s*([\w.]+)\.self\s*,\s*\"(\w+)\"", src):
        out.setdefault(m.group(2), [])
        if m.group(1) not in out[m.group(2)]:
            out[m.group(2)].append(m.group(1))
    return out


def _find(name, near=None):
    sw = swift()
    if near is not None:
        return sw.find_type(name, near.file, near.a + 1)
    cands = [t for t in sw.types_by_name.get(name.split(".")[-1], []) if t.kind != "extension"]
    if "." in name:
        cands = [t for t in cands if t.qual.endswith(name)] or cands
    return cands[0] if len(cands) == 1 else (sw.find_type(name, cands[0].file, cands[0].a) if cands else None)


def _strip_optional(typ):
    """(bare type, optional)."""
    t = re.sub(r"\s+", "", typ or "")
    if t.startswith("Optional<") and t.endswith(">"):
        return t[len("Optional<"):-1], True
    if t.endswith(("?", "!")):
        return t[:-1], True
    return t, False


def _split_collection(t):
    """("array", element) / ("dict", value) / (None, t)."""
    if t.startswith("[") and t.endswith("]"):
        inner, depth = t[1:-1], 0
        for i, c in enumerate(inner):
            if c in "[<(":
                depth += 1
            elif c in "]>)":
                depth -= 1
            elif c == ":" and depth == 0:
                return "dict", inner[i + 1:]
        return "array", inner
    m = re.match(r"^(?:Array|ContiguousArray)<(.+)>$", t)
    if m:
        return "array", m.group(1)
    return None, t


def _custom_init(t):
    sw = swift()
    for part in sw.type_parts(t):
        for fn in part.file.funcs:
            if fn.name == "init" and part.a < fn.start < part.b and fn.params and fn.params[0][0] == "from" \
                    and sw.enclosing_type(part.file, fn.start) is part:
                return fn
    return None


def _single_value(t):
    """Whether the type reads the whole value itself (a single-value or
    unkeyed container in its own init(from:)) — it decides what a null or an
    odd shape is, so nothing here judges it."""
    fn = _custom_init(t) if t is not None else None
    if fn is None:
        return False
    body = fn.file.masked[fn.body_a:fn.body_b]
    return "singleValueContainer" in body or "unkeyedContainer" in body


_GUARD = re.compile(r"\bguard\b(.*?)\belse\s*\{", re.S)
_GUARD_LET = re.compile(r"\blet\s+\w+\s*=\s*\(?\s*try\?\s*[A-Za-z_]\w*\.decode\((?:[^()]|\([^()]*\))*?forKey:\s*\.(\w+)")


def _guarded_required(fn):
    """Keys bound by `guard let x = try? c.decode(…, forKey: .k) else {
    throw … }`: a nil there throws, so the key is required after all."""
    out = set()
    skel, masked = fn.file.skel, fn.file.masked
    body = masked[fn.body_a:fn.body_b]
    for m in _GUARD.finditer(body):
        brace = fn.body_a + m.end() - 1
        close = bp.match_close(skel, brace)
        if close > 0 and "throw" in masked[brace:close]:
            out |= set(_GUARD_LET.findall(m.group(1)))
    return out


_PROPS = {}


def props(t):
    """{json key: (type text, verdict)} for the type declaration `t`;
    verdict is "required" (a null fails), "optional" (a null at the key is
    fine, but what is under it is still read strictly) or "lenient" (nothing
    under it can fail the decode)."""
    if t is None:
        return {}
    key = (t.file.rel, t.a)
    if key not in _PROPS:
        _PROPS[key] = _props(t)
    return _PROPS[key]


def _props(t):
    sw = swift()
    ck = bp._coding_keys(sw, t)
    declared = {}
    for part in sw.type_parts(t):
        top = bp._top_level(part.file, part.a, part.b)
        for m in bp._PROP_RE.finditer(top):
            mods, kind, name, _, typ, tail = m.groups()
            if re.search(r"\b(static|class|lazy)\b", mods or ""):
                continue
            if kind == "var" and tail == "{}":
                continue
            declared.setdefault(name, ((typ or "").strip(), kind, tail == "=", mods or ""))
    out = {}
    fn = _custom_init(t)
    if fn is not None:
        body = fn.file.masked[fn.body_a:fn.body_b]
        guarded = _guarded_required(fn)
        for case in guarded & set(ck):
            out[ck[case]] = (declared.get(case, ("", "", False, ""))[0], "required")
        for line in body.split("\n"):
            for case in re.findall(r"forKey:\s*\.(\w+)", line) + re.findall(r"\(\s*\.(\w+)\s*[,)]", line):
                if case not in ck:
                    continue
                typ = declared.get(case, ("", "", False, ""))[0]
                if "try?" in line:
                    verdict = "lenient"
                elif "decodeIfPresent" in line:
                    verdict = "optional"
                elif _DECODE_STRICT.search(line) and "forKey" in line:
                    verdict = "required"
                else:
                    verdict = "lenient"          # a helper reads it (c.setupText(.k))
                prev = out.get(ck[case])
                if prev is None or {"required": 2, "optional": 1, "lenient": 0}[verdict] > \
                        {"required": 2, "optional": 1, "lenient": 0}[prev[1]]:
                    out[ck[case]] = (typ, verdict)
        return out
    keys = ck if ck else {name: name for name in declared}
    for case, raw in keys.items():
        d = declared.get(case)
        if d is None:
            continue
        typ, kind, has_default, mods = d
        if kind == "let" and has_default:
            continue                             # never decoded
        bare, optional = _strip_optional(typ)
        if "@" in mods:
            verdict = "lenient"                  # a property wrapper decodes it
        elif optional:
            verdict = "optional"
        else:
            verdict = "required"
        out[raw] = (typ, verdict)
    return out


def _resolve(type_text, near):
    """The Swift type declaration a property's (bare) type names, or None
    for a leaf or anything this cannot follow."""
    if not type_text or type_text in _LEAVES or "<" in type_text:
        return None
    return _find(type_text, near)


def _tokens(path):
    return re.findall(r"\.([^.\[]+)|(\[\])", path[1:] if path.startswith("$") else path)


def requirement(route, path):
    """(required?, why) for a null at `path` (e.g. "$.user.username",
    "$.modules[].count", "$.rows[]") in the payload of fixture `route`."""
    for root in root_types().get(route, []):
        why = _walk(_find(root), _tokens(path), root)
        if why:
            return True, why
    return False, None


def _walk(t, tokens, label):
    if t is None:
        return None
    node = ("type", t)
    for i, (key, arr) in enumerate(tokens):
        last = i == len(tokens) - 1
        kind, val = node
        if kind == "expr":
            bare, optional = _strip_optional(val[0])
            coll, inner = _split_collection(bare)
            if arr:
                if coll != "array":
                    return None
                elem, elem_opt = _strip_optional(inner)
                if last:
                    return None if elem_opt else f"{label}: an element of {val[0]} is a non-optional {elem}"
                node = ("expr", (elem, val[1]))
                continue
            if coll == "dict":
                return None                      # data keys: never a schema key
            nxt = _resolve(bare, val[1])
            if nxt is None or _single_value(nxt):
                return None
            node = ("type", nxt)
        kind, t = node
        if arr:
            return None
        p = props(t).get(key)
        if p is None:
            return None                          # no model reads this key
        typ, verdict = p
        if verdict == "lenient":
            return None
        if last:
            if verdict != "required":
                return None
            bare, _ = _strip_optional(typ)
            target = _resolve(_split_collection(bare)[1], t) if not bare.startswith("[") else None
            if target is not None and _single_value(target):
                return None                      # its own init decides what a null is
            return f"{t.qual}.{key} is a non-optional {typ}"
        node = ("expr", (typ, t))
        label = f"{t.qual}.{key}"
    return None


# ── a decode, simulated ─────────────────────────────────────────────────────
#
# What JSONDecoder would say of a payload, from the same reading of the
# models: a required key missing or null, a string where a number was held,
# a fraction where an Int was. Approximate on purpose — a custom init(from:)
# is followed only as far as `props` reads it, and an enum only when its raw
# values are literal — so it is a development check (and a Python mirror of
# ResponseShapeContractTests while the iOS lane cannot run), never the
# authority: the XCTest decodes with the real models.

_NUM = (int, float)


def _kind_problem(bare, value, near, path, depth):
    if bare in ("String", "URL", "Date", "UUID"):
        return None if isinstance(value, str) else f"{path}: a {type(value).__name__} where {bare} is held"
    if bare in ("Int", "Int64", "UInt"):
        if isinstance(value, bool) or not isinstance(value, _NUM):
            return f"{path}: a {type(value).__name__} where {bare} is held"
        return None if float(value).is_integer() else f"{path}: {value!r} where {bare} is held"
    if bare in ("Double", "Float", "CGFloat", "Decimal"):
        return None if isinstance(value, _NUM) and not isinstance(value, bool) else \
            f"{path}: a {type(value).__name__} where {bare} is held"
    if bare == "Bool":
        return None if isinstance(value, bool) else f"{path}: a {type(value).__name__} where Bool is held"
    coll, inner = _split_collection(bare)
    if coll == "array":
        if not isinstance(value, list):
            return f"{path}: a {type(value).__name__} where {bare} is held"
        elem, opt = _strip_optional(inner)
        for i, x in enumerate(value):
            if x is None:
                if not opt:
                    return f"{path}[{i}]: null where {elem} is held"
                continue
            p = _kind_problem(elem, x, near, f"{path}[{i}]", depth)
            if p:
                return p
        return None
    if coll == "dict":
        if not isinstance(value, dict):
            return f"{path}: a {type(value).__name__} where {bare} is held"
        val, opt = _strip_optional(inner)
        for k, x in value.items():
            if x is None:
                if not opt:
                    return f"{path}.{k}: null where {val} is held"
                continue
            p = _kind_problem(val, x, near, f"{path}.{k}", depth)
            if p:
                return p
        return None
    t = _resolve(bare, near)
    if t is None:
        return None                              # a generic or a type this cannot follow
    return decode_problem(t, value, path, depth + 1)


def decode_problem(t, value, path="$", depth=0):
    """The first thing JSONDecoder would refuse decoding `value` as the type
    declaration `t`, or None."""
    if depth > 40 or t is None or _single_value(t):
        return None
    if t.kind == "enum":
        if _custom_init(t) is not None:
            return None
        raw = re.search(r"\benum\s+\w+\s*:\s*(String|Int)\b", t.file.skel[max(0, t.a - 300):t.a + 1])
        if not raw:
            return None
        cases = set()
        for cm in re.finditer(r"\bcase\s+([^\n;{]+)", bp._top_level(t.file, t.a, t.b)):
            for item in cm.group(1).split(","):
                im = re.match(r'\s*`?(\w+)`?\s*(?:=\s*"([^"]*)")?', t.file.masked[0:0] + item)
                if im:
                    cases.add(im.group(2) if im.group(2) is not None else im.group(1))
        if raw.group(1) == "String":
            if not isinstance(value, str):
                return f"{path}: a {type(value).__name__} where {t.qual} (a String enum) is held"
            # Raw values written as string literals are blanked in the
            # skeleton: read them from the source text instead.
            src_cases = set()
            for cm in re.finditer(r'\bcase\s+([^\n;{]+)', t.file.src[t.a:t.b]):
                for item in cm.group(1).split(","):
                    im = re.match(r'\s*`?(\w+)`?\s*(?:=\s*"([^"]*)")?', item)
                    if im:
                        src_cases.add(im.group(2) if im.group(2) is not None else im.group(1))
            if src_cases and value not in src_cases:
                return f"{path}: {value!r} is not a case of {t.qual}"
        return None
    if not isinstance(value, dict):
        return f"{path}: a {type(value).__name__} where {t.qual} (an object) is held"
    for key, (typ, verdict) in props(t).items():
        if verdict == "lenient":
            continue
        q = f"{path}.{key}"
        if key not in value:
            if verdict == "required":
                return f"{q}: missing — {t.qual}.{key} is a non-optional {typ}"
            continue
        x = value[key]
        if x is None:
            if verdict == "required":
                return f"{q}: null — {t.qual}.{key} is a non-optional {typ}"
            continue
        bare, _opt = _strip_optional(typ)
        if not bare:
            continue
        p = _kind_problem(bare, x, t, q, depth)
        if p:
            return p
    return None


def route_decode_problems(route, payload):
    """[problem] for each model ResponseShapeContractTests decodes `route`
    as, simulated on `payload`."""
    out = []
    for root in root_types().get(route, []):
        p = decode_problem(_find(root), payload)
        if p:
            out.append(f"{root}: {p}")
    return out
