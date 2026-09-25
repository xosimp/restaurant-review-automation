"""The product is "Cavnar AI" in everything a user reads — never bare "Cavnar".

A ratchet: the owner asked for it, a sweep fixed every surface, and this
keeps it fixed. It reads

  * every template in templates/ (text, attributes and inline JS strings;
    Jinja, HTML, CSS and JS comments are skipped),
  * every Swift source in the app, the widget and the shared target (string
    literals only; comments are skipped), and
  * the string literals of the owner-facing Python modules, with the
    tokenize module (docstrings and comments are skipped).

"Bare" means the word Cavnar that is not followed by " AI", is not the
owner's surname ("Will Cavnar"), and is not part of an identifier, a domain
or an email address (CavnarAI, cavnar_*, cavnar-*, cavnar.ai, @cavnar.ai).
"""
import io
import os
import re
import tokenize

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The word itself, bare: no identifier character, dot, slash, dash or @ on
# either side (a domain, a path, a CSS class, an identifier), not ".ai", and
# not already followed by " AI" (the wordmark "Cavnar <em>AI</em>" counts).
BARE = re.compile(r"(?<![A-Za-z0-9_\-./@#])Cavnar(?![A-Za-z0-9_\-@])(?!\.ai\b)(?!\s*(?:<[^>]*>\s*)*AI\b)")
SURNAME = re.compile(r"\bWill\s+Cavnar\b")

# (path relative to the repo root, text that must appear in the flagged
# line or literal): matching code, never text a user reads. Keep it tiny;
# each entry says why.
ALLOWLIST = [
    # Schedule rows the repair loop touched carry "Cavnar AI: …"; rows saved
    # before the rename carry "Cavnar: …". Both readers accept either.
    ("templates/dashboard.html", "/^Cavnar( AI)?:/.test("),
    ("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift", "Cavnar:"),
    # Tests a server label ("… on Cavnar AI", or "… on Cavnar" from a row
    # written before the rename) before adding the platform line again.
    ("templates/dashboard.html", "!/on Cavnar/i.test(bs)"),
]


def _allowed(path, text):
    rel = os.path.relpath(path, ROOT)
    return any(rel == p and snippet in text for p, snippet in ALLOWLIST)


def _bare(text):
    return BARE.findall(SURNAME.sub("", text))


def _line(src, pos):
    return src.count("\n", 0, pos) + 1


# ── templates ────────────────────────────────────────────────────────────

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _blank(m):
    # Keep the newlines so reported line numbers stay right.
    return re.sub(r"[^\n]", " ", m.group(0))


def _strip_line_comment(line):
    """A JS `//` comment, when the `//` is outside any quote (not a URL)."""
    q = None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "/" and line[i:i + 2] == "//" and (i == 0 or line[i - 1] != ":"):
            return line[:i]
        i += 1
    return line


def _template_hits(path):
    src = open(path, encoding="utf-8").read()
    for rx in (_JINJA_COMMENT, _HTML_COMMENT, _BLOCK_COMMENT):
        src = rx.sub(_blank, src)
    hits = []
    for n, line in enumerate(src.split("\n"), 1):
        code = _strip_line_comment(line)
        if _bare(code) and not _allowed(path, code):
            hits.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()[:140]}")
    return hits


def _templates():
    d = os.path.join(ROOT, "templates")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".html"))


def test_templates_say_cavnar_ai():
    hits = [h for p in _templates() for h in _template_hits(p)]
    assert not hits, "bare \"Cavnar\" in a template (say \"Cavnar AI\"):\n" + "\n".join(hits[:40])


# ── Swift ────────────────────────────────────────────────────────────────

def _swift_strings(src):
    """(offset, text) of every string-literal segment in Swift source —
    plain, raw (#"…"#) and multi-line — with comments skipped and the code
    inside an interpolation \\( … ) read as code again."""
    out = []
    i, n = 0, len(src)
    # A stack of open contexts: ("code", paren_depth) inside an
    # interpolation, ("str", hashes, multiline) inside a literal.
    stack = []

    def in_string():
        return stack and stack[-1][0] == "str"

    seg_start = None
    while i < n:
        if in_string():
            _, hashes, multi = stack[-1]
            close = ('"""' if multi else '"') + "#" * hashes
            esc = "\\" + "#" * hashes
            if src.startswith(close, i):
                out.append((seg_start, src[seg_start:i]))
                stack.pop()
                i += len(close)
                continue
            if src.startswith(esc + "(", i):
                out.append((seg_start, src[seg_start:i]))
                stack.append(("code", 0))
                i += len(esc) + 1
                continue
            if src.startswith(esc, i):
                i += len(esc) + 1
                continue
            i += 1
            continue
        # code
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if src.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif src.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        m = re.match(r'(#*)("""|")', src[i:i + 12])
        if m:
            hashes, quote = len(m.group(1)), m.group(2)
            stack.append(("str", hashes, quote == '"""'))
            i += m.end()
            seg_start = i
            continue
        c = src[i]
        if stack and stack[-1][0] == "code":
            if c == "(":
                stack[-1] = ("code", stack[-1][1] + 1)
            elif c == ")":
                if stack[-1][1] == 0:
                    stack.pop()          # back into the enclosing literal
                    i += 1
                    seg_start = i
                    continue
                stack[-1] = ("code", stack[-1][1] - 1)
        i += 1
    return out


def _swift_files():
    base = os.path.join(ROOT, "ios", "CavnarAI")
    out = []
    for sub in ("CavnarAI", "CavnarWidgets", "Shared"):
        for d, _dirs, files in os.walk(os.path.join(base, sub)):
            out += [os.path.join(d, f) for f in files if f.endswith(".swift")]
    return sorted(out)


def test_swift_scanner_reads_strings_not_comments():
    src = ('// Cavnar comment\n/* Cavnar */ let a = "Cavnar reads" + #"raw Cavnar"#\n'
           'let b = "x \\(f("Cavnar inner")) Cavnar AI" ; let c = """\nCavnar multi\n"""\n'
           'let d = "Will Cavnar"')
    bare = [t for _o, t in _swift_strings(src) if _bare(t)]
    assert bare == ["Cavnar reads", "raw Cavnar", "Cavnar inner", "\nCavnar multi\n"]


def test_swift_says_cavnar_ai():
    assert _swift_files(), "no Swift sources found"
    hits = []
    for p in _swift_files():
        src = open(p, encoding="utf-8").read()
        for off, text in _swift_strings(src):
            if _bare(text) and not _allowed(p, text):
                hits.append(f"{os.path.relpath(p, ROOT)}:{_line(src, off)}: {text.strip()[:140]}")
    assert not hits, "bare \"Cavnar\" in a Swift string (say \"Cavnar AI\"):\n" + "\n".join(hits[:40])


# ── Python ───────────────────────────────────────────────────────────────

PY_MODULES = [
    "emails.py", "reporter.py", "notify.py", "morning_brief.py", "home_brief.py",
    "client_api.py", "mobile_api.py",
]
PY_PACKAGES = ["dsr"]


def _py_files():
    out = [os.path.join(ROOT, m) for m in PY_MODULES]
    for pkg in PY_PACKAGES:
        d = os.path.join(ROOT, pkg)
        out += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py")]
    return sorted(out)


def _py_literals(src):
    """(line, text) of every string literal that is not a docstring. An
    f-string is read as its literal parts."""
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    lines = src.splitlines(keepends=True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    kinds = {tokenize.STRING}
    for name in ("FSTRING_MIDDLE", "TSTRING_MIDDLE"):
        if hasattr(tokenize, name):
            kinds.add(getattr(tokenize, name))
    ignorable = {tokenize.NL, tokenize.COMMENT}
    prev = None
    out = []
    for t in toks:
        if t.type in kinds:
            doc = t.type == tokenize.STRING and prev in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
            if not doc:
                a = starts[t.start[0] - 1] + t.start[1]
                b = starts[t.end[0] - 1] + t.end[1]
                out.append((t.start[0], src[a:b]))
        if t.type not in ignorable:
            prev = t.type
    return out


def _py_hits(path):
    rel = os.path.relpath(path, ROOT)
    src = open(path, encoding="utf-8").read()
    hits = []
    for line, text in _py_literals(src):
        if _bare(text) and not _allowed(path, text):
            hits.append(f"{rel}:{line}: {text.strip()[:140]}")
    return hits


def test_python_scanner_skips_docstrings_and_comments():
    src = ('"""Cavnar module doc."""\n# Cavnar comment\ndef f():\n    """Cavnar doc."""\n'
           '    x = "Cavnar reads"\n    y = f"{x} Cavnar\'s range"\n    z = "Will Cavnar, Cavnar AI, cavnar.ai"\n')
    bare = [t for _l, t in _py_literals(src) if _bare(t)]
    assert len(bare) == 2 and "Cavnar reads" in bare[0] and "range" in bare[1]


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: os.path.relpath(p, ROOT))
def test_python_says_cavnar_ai(path):
    hits = _py_hits(path)
    assert not hits, "bare \"Cavnar\" in a user-facing string (say \"Cavnar AI\"):\n" + "\n".join(hits[:40])
