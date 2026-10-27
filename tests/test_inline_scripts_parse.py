"""Every inline <script> in templates/*.html parses, and the pages the ES5
rule covers stay ES5 (release-safety audit 10/26/26, A01-14 / A06-06).

A stray brace or a stray `,` in one block is a SyntaxError: the whole block
never runs, so every panel it drives sits on its placeholder for every
owner — while Jinja still parses the template and the regex lint in
tests/test_frontend_rules.py (backticks, const/let, =>, async/await) still
passes. A construct that regex cannot see (a default parameter, a spread, a
trailing comma in a call) does the same in an older browser. This parses
each block for real.

How:
- Every `<script>` with no `src` and a JavaScript type, in every template
  (the includes too: `_csrf_fetch.html` and `_fc_receive_js.html` hold their
  own blocks).
- Jinja neutralised with line numbers kept: comments and statements blank,
  each `{{ … }}` becomes `0` (a valid expression in code, harmless inside a
  string). A block with `{% if %}`/`{% for %}` is parsed twice — once keeping
  the first branch of every conditional, once keeping the last (`else`) —
  so code that only one branch renders is parsed too.
- One node process for every block, the program on stdin
  (tests/test_frontend_rules.py: test_no_test_hands_node_its_script_as_an_argument).
  The parser is the acorn node ships inside itself (`--expose-internals`):
  `ecmaVersion: "latest"` for every block, `ecmaVersion: 5` for the ES5
  pages. Where that copy is unavailable the syntax check falls back to
  `vm.Script` and the ES5 check to the regex lint, and says so.

The scripts in /static (blind re-audit 10/27/26): every `static/**/*.js` is
parsed the same way, whole, and held to ES5 — each is loaded by a page an
owner, an employee or a guest opens (login, the dashboard, the staff
sign-in, the guest opt-in), and a `?.` in one breaks that page in an older
browser just as an inline block would.

The ES5 rule (CLAUDE.md "Inline JS is ES5 only") holds for every page an
owner, an employee or a guest opens. The internal pages below — the admin
console and the sales audit tool, opened by Will in a current browser — use
template literals and arrows today, so they are held to "parses" only; an
entry that has become ES5-clean fails, so the list only shrinks.
"""
import functools
import glob
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")

# Internal pages exempt from ES5 (they must still parse), and who renders them.
MODERN_JS_OK = {
    "admin.html": "the admin console (admin_routes)",
    "client_data.html": "the admin console's client data page (admin_routes.client_data)",
    "client_settings.html": "the admin console's legacy client-settings page (admin_routes)",
    "audit_list.html": "the in-person sales audit tool (sales_audit_routes)",
    "audit_tool.html": "the in-person sales audit tool (sales_audit_routes)",
    "audit_cheatsheet.html": "the in-person sales audit tool (sales_audit_routes)",
}

_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)
_JINJA = re.compile(r"\{#.*?#\}|\{%-?\s*(?P<kw>\w+)?.*?-?%\}|\{\{.*?\}\}", re.S)
_JS_TYPES = ("", "text/javascript", "application/javascript", "module")
# The regex lint, used only when node's own acorn is not available.
_ES6_TOKENS = [
    (r"`", "template literal"),
    (r"\bconst\s", "const"),
    (r"\blet\s", "let"),
    (r"=>", "arrow function"),
    (r"\basync\s+function\b", "async function"),
    (r"\bawait\s", "await"),
]


def _newlines(text):
    return "\n" * text.count("\n")


def neutralise(body, branch="first"):
    """`body` with its Jinja replaced, every line where it was. `branch`
    "first" keeps the first branch of each `if`/`for … else`; "last" keeps
    the `else` branch (or nothing, for an `if` without one)."""
    out, pos, stack = [], 0, []

    def skipping():
        return any(stack)

    for m in _JINJA.finditer(body):
        seg = body[pos:m.start()]
        out.append(_newlines(seg) if skipping() else seg)
        tok, pos = m.group(0), m.end()
        if tok.startswith("{{"):
            out.append(_newlines(tok) if skipping() else "0" + _newlines(tok))
            continue
        kw = m.group("kw") or ""
        if tok.startswith("{%"):
            if kw in ("if", "for"):
                stack.append(branch == "last")
            elif kw in ("elif", "else") and stack:
                stack[-1] = True if branch == "first" else (kw == "elif")
            elif kw in ("endif", "endfor") and stack:
                stack.pop()
        out.append(_newlines(tok))
    seg = body[pos:]
    out.append(_newlines(seg) if skipping() else seg)
    return "".join(out)


def inline_blocks(name, source):
    """[{name, line, branch, src, es5, module}] for each inline JavaScript
    block in one template's source; `line` is the template line the
    block's first line is on."""
    blocks = []
    for m in _SCRIPT.finditer(source):
        attrs = m.group("attrs") or ""
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        t = re.search(r"\btype\s*=\s*([\"'])(.*?)\1", attrs, re.I)
        js_type = (t.group(2).strip().lower() if t else "")
        if js_type not in _JS_TYPES:
            continue
        body = m.group("body")
        line = source.count("\n", 0, m.start("body")) + 1
        has_blocks = re.search(r"\{%-?\s*(if|for)\b", body) is not None
        for branch in (("first", "last") if has_blocks else ("first",)):
            blocks.append({"name": name, "line": line, "branch": branch, "src": neutralise(body, branch),
                           "es5": name not in MODERN_JS_OK and js_type != "module",
                           "module": js_type == "module"})
    return blocks


# One node program for every block: the blocks ride in the program itself
# (stdin), so no argument carries a script and no temp file is needed.
_NODE_CHECK = r"""
var acorn = null;
if (!FORCE_VM) {
  try { acorn = require('internal/deps/acorn/acorn/dist/acorn'); } catch (e) { acorn = null; }
}
var vm = require('vm');
var out = {mode: acorn ? 'acorn' : 'vm', results: []};
BLOCKS.forEach(function (b) {
  var r = {i: b.i};
  try {
    if (acorn) acorn.parse(b.src, {ecmaVersion: 'latest', sourceType: b.module ? 'module' : 'script'});
    else if (!b.module) new vm.Script(b.src, {filename: b.name});
  } catch (e) {
    r.syntax = String(e.message);
    /* acorn names the line; vm.Script puts "<filename>:<line>" atop the stack. */
    var m = /^[^\n]*:(\d+)\s*\n/.exec(String(e.stack || ''));
    r.at = e.loc ? e.loc.line : (m ? +m[1] : null);
  }
  if (acorn && b.es5 && !r.syntax) {
    try { acorn.parse(b.src, {ecmaVersion: 5, sourceType: 'script'}); }
    catch (e2) { r.es5 = String(e2.message); r.at = e2.loc ? e2.loc.line : null; }
  }
  out.results.push(r);
});
process.stdout.write(JSON.stringify(out));
"""


def _node():
    node = shutil.which("node")
    if not node:
        if os.getenv("CI"):
            pytest.fail("node is not on PATH: the inline-script parse gate cannot run")
        pytest.skip("node is not installed")
    return node


def check_blocks(blocks, force_vm=False):
    """(mode, failures): mode "acorn" or "vm"; each failure is
    (kind, "<template>:<line> [branch] <message>", block) with kind
    "syntax" or "es5". `force_vm` runs the fallback (for its own test)."""
    node = _node()
    payload = [{"i": i, "name": b["name"], "src": b["src"], "es5": b["es5"], "module": b["module"]}
               for i, b in enumerate(blocks)]
    program = ("var FORCE_VM = %s;\nvar BLOCKS = " % ("true" if force_vm else "false")
               + json.dumps(payload) + ";\n" + _NODE_CHECK)
    out = None
    for flags in (["--expose-internals"], []):
        proc = subprocess.run([node] + flags + ["-"], input=program, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            out = json.loads(proc.stdout)
            break
        if "bad option" not in (proc.stderr or ""):
            raise AssertionError("node could not run the parse check:\n" + (proc.stderr or "")[-2000:])
    assert out is not None, "node refused to run the parse check"
    failures = []
    for r in out["results"]:
        b = blocks[r["i"]]
        where = "%s:%s" % (b["name"], b["line"] + (r.get("at") or 1) - 1)
        tag = "" if b["branch"] == "first" else " [else branch]"
        if r.get("syntax"):
            failures.append(("syntax", "%s%s %s" % (where, tag, r["syntax"]), b))
        elif r.get("es5"):
            failures.append(("es5", "%s%s %s" % (where, tag, r["es5"]), b))
    if out["mode"] != "acorn":
        for b in blocks:
            if not b["es5"]:
                continue
            for i, text in enumerate(b["src"].split("\n")):
                for pat, label in _ES6_TOKENS:
                    if re.search(pat, text):
                        failures.append(("es5", "%s:%s %s (regex lint)" % (b["name"], b["line"] + i, label), b))
    return out["mode"], failures


def _template_names():
    return sorted(os.path.relpath(p, TEMPLATES)
                  for p in glob.glob(os.path.join(TEMPLATES, "**", "*.html"), recursive=True))


@functools.lru_cache(maxsize=None)
def _repo_check():
    blocks = []
    for name in _template_names():
        with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
            blocks.extend(inline_blocks(name, fh.read()))
    mode, failures = check_blocks(blocks)
    return blocks, mode, failures


STATIC = os.path.join(ROOT, "static")


def static_blocks():
    """One block per `static/**/*.js` file, held to ES5."""
    blocks = []
    for path in sorted(glob.glob(os.path.join(STATIC, "**", "*.js"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        blocks.append({"name": os.path.relpath(path, ROOT), "line": 1, "branch": "first", "src": src,
                       "es5": True, "module": False})
    return blocks


def test_every_static_script_parses_and_stays_es5():
    blocks = static_blocks()
    names = {b["name"] for b in blocks}
    for must in ("static/cavnar-phone.js", "static/ember-core.js", "static/cavnar-field.js"):
        assert must in names, must
    mode, failures = check_blocks(blocks)
    bad = [msg for _kind, msg, _b in failures]
    assert not bad, ("a /static script does not parse, or is not ES5 (%s) — every page that loads it "
                     "breaks:\n" % mode + "\n".join(bad[:20]))


def test_the_static_scan_catches_a_broken_file(tmp_path, monkeypatch):
    (tmp_path / "ok.js").write_text("var a = 1;\n")
    (tmp_path / "es6.js").write_text("var a = {b: 1};\nvar c = a?.b;\n")
    monkeypatch.setattr(__import__(__name__), "STATIC", str(tmp_path))
    blocks = static_blocks()
    mode, failures = check_blocks(blocks)
    assert [os.path.basename(b["name"]) for _k, _m, b in failures] == ["es6.js"], failures


def test_every_static_script_a_template_loads_exists():
    missing = []
    for name in _template_names():
        with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
            for m in re.finditer(r"""src=["'](?:\{\{[^}]*\}\})?/static/([\w./-]+\.js)""", fh.read()):
                if not os.path.exists(os.path.join(STATIC, m.group(1))):
                    missing.append(f"{name}: /static/{m.group(1)}")
    assert not missing, missing


def test_the_scan_finds_the_dashboard_and_every_include():
    blocks, _mode, _f = _repo_check()
    names = {b["name"] for b in blocks}
    for must in ("dashboard.html", "_csrf_fetch.html", "_fc_receive_js.html", "staff_portal.html", "login.html"):
        assert must in names, must
    assert sum(1 for b in blocks if b["name"] == "dashboard.html") > 30


def test_every_inline_script_parses():
    _blocks, mode, failures = _repo_check()
    bad = [msg for kind, msg, _b in failures if kind == "syntax"]
    assert not bad, ("an inline <script> does not parse (%s) — the whole block would never run:\n" % mode
                     + "\n".join(bad[:20]))


def test_owner_staff_and_guest_pages_stay_es5():
    _blocks, mode, failures = _repo_check()
    bad = [msg for kind, msg, _b in failures if kind == "es5"]
    assert not bad, ("ES5 only on every page an owner, an employee or a guest opens (CLAUDE.md) — "
                     "found (%s):\n" % mode + "\n".join(bad[:20]))


def test_the_es5_exemptions_are_still_needed():
    """MODERN_JS_OK only shrinks: an internal page that is now ES5-clean
    leaves the list, so the rule covers it from then on."""
    blocks, mode, _f = _repo_check()
    if mode != "acorn":
        pytest.skip("needs node's acorn to tell ES5 from later syntax")
    for name in MODERN_JS_OK:
        assert os.path.exists(os.path.join(TEMPLATES, name)), "%s is gone: drop it from MODERN_JS_OK" % name
    mine = [dict(b, es5=True) for b in blocks if b["name"] in MODERN_JS_OK]
    _m, failures = check_blocks(mine)
    still = {b["name"] for kind, _msg, b in failures if kind == "es5"}
    clean = sorted(set(MODERN_JS_OK) - still)
    assert not clean, "now ES5-clean, so held to the ES5 rule: remove from MODERN_JS_OK: %s" % clean


# ── the gate itself ─────────────────────────────────────────────────────────

def test_the_gate_fails_a_broken_block_and_names_its_line():
    page = ("<html>\n<body>\n<script>\nvar ok = 1;\n</script>\n"
            "<script>\nfunction load() {\n  var a = [1, 2;\n}\n</script>\n</body>\n</html>\n")
    mode, failures = check_blocks(inline_blocks("broken.html", page))
    assert [kind for kind, _m, _b in failures] == ["syntax"], failures
    assert failures[0][1].startswith("broken.html:8 "), (mode, failures[0][1])


@pytest.mark.parametrize("snippet", [
    "function f(a, b) { return a; }\nf(1, 2,);",          # trailing comma in a call (ES2017)
    "function f(a, b = 1) { return a + b; }",            # default parameter
    "var a = [1, 2]; var b = [0].concat(...a);",         # spread
    "var o = {a: 1}; var p = {...o};",                   # object spread
    "for (var x of [1, 2]) {}",                          # for-of
    "var s = 'a'; var o = {s};",                         # shorthand property
    "class A {}",                                        # class
    "var t = `x`;",                                      # template literal (also the regex lint)
])
def test_es5_check_catches_what_the_regex_lint_cannot(snippet):
    page = "<script>\n" + snippet + "\n</script>\n"
    mode, failures = check_blocks(inline_blocks("es6.html", page))
    if mode != "acorn":
        pytest.skip("needs node's acorn")
    assert [kind for kind, _m, _b in failures] == ["es5"], failures
    _m2, internal = check_blocks(inline_blocks("admin.html", page))
    assert internal == [], "an internal page is held to parsing only"


def test_jinja_is_neutralised_and_both_branches_are_parsed():
    good = ("<script>\nvar id = {{ restaurant.id }}, name = {{ name|tojson }};\n"
            "{# a comment #}\nvar on = '{{ flag }}';\n"
            "{% if x %}var mode = 'a';{% elif y %}var mode = 'b';{% else %}var mode = null;{% endif %}\n"
            "var rows = [{% for r in rows %}{n: {{ r.n }}},{% endfor %}];\n</script>\n")
    _m, failures = check_blocks(inline_blocks("ok.html", good))
    assert failures == [], failures
    # Only the else branch is broken: the "last" pass finds it.
    bad_else = "<script>\n{% if x %}\nvar a = 1;\n{% else %}\nvar a = ;\n{% endif %}\n</script>\n"
    _m, failures = check_blocks(inline_blocks("else.html", bad_else))
    assert len(failures) == 1 and "[else branch]" in failures[0][1], failures
    assert failures[0][1].startswith("else.html:5 "), failures[0][1]
    # Only the first branch is broken: the "first" pass finds it.
    bad_if = "<script>\n{% if x %}\nvar a = ;\n{% else %}\nvar a = 1;\n{% endif %}\n</script>\n"
    _m, failures = check_blocks(inline_blocks("if.html", bad_if))
    assert len(failures) == 1 and "[else branch]" not in failures[0][1] and \
        failures[0][1].startswith("if.html:3 "), failures


def test_the_fallback_still_catches_a_broken_block_and_the_regex_tokens():
    """Where node's acorn is unavailable: vm.Script for syntax, the regex
    lint for ES5 — weaker, and the mode says so, but never silent."""
    page = "<script>\nvar t = `x`;\n</script>\n<script>\nvar a = (;\n</script>\n"
    mode, failures = check_blocks(inline_blocks("fallback.html", page), force_vm=True)
    assert mode == "vm"
    kinds = sorted((kind, msg.split(" ")[0]) for kind, msg, _b in failures)
    assert kinds == [("es5", "fallback.html:2"), ("syntax", "fallback.html:5")], failures


def test_non_javascript_blocks_and_external_scripts_are_skipped():
    page = ('<script src="/static/x.js"></script>\n'
            '<script type="application/ld+json">{"@context": "https://schema.org"}</script>\n'
            '<script type="text/template"><div>{{ x }}</div></script>\n')
    assert inline_blocks("skip.html", page) == []
