#!/usr/bin/env python3
"""
check_owner_copy.py — hand-written owner copy may not use the labels the
"Never Say" audit banned (9/24/26, NS1 V7 and NS3 R9).

response_validation.py checks what a MODEL writes. The same audit found that
most of the overclaiming an owner reads was written by us: the gap above
the labor target shown as "Monthly savings", "Value delivered … measured"
with no caveat, "Waste you can claw back … the one number to remember", an
"AI-Optimized Schedule" nothing optimises, "High confidence" as a word where
the product shows a percentage. This lint reads the string literals and
page text of the owner-facing sources and counts those labels.

It is a RATCHET, like check_email_tokens.py: the count may never rise.
Clients-copy work (workstreams W/C) drives it to zero and lowers BASELINE as
it goes. A line that must keep a phrase (a prompt telling the model not to
write it, a test fixture) carries the marker `owner-copy-ok` on that line.

What is read:
  * Python (emails.py, reporter.py, notify.py, strategy_jobs.py,
    milestones.py, value_delivered.py, sales_audit_cheatsheet.py): string
    literals only — never comments, never docstrings.
  * Swift (ios/**/*.swift): string literals only, never // comments.
  * HTML (templates/**, public/**): the page text and inline script
    strings, without <!-- --> comments or // and /* */ comment lines.

Run: python3 scripts/check_owner_copy.py [--list]   (exit 1 when over BASELINE)
"""
import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_FILES = ["emails.py", "reporter.py", "notify.py", "strategy_jobs.py", "milestones.py", "value_delivered.py",
            "sales_audit_cheatsheet.py"]
HTML_DIRS = ["templates", "public"]
SWIFT_DIR = "ios"

# The banned labels: NS1 V7 (owner UI copy) and NS3 R9 (labels over an
# opportunity, benchmark or projection figure).
BANNED = [
    ("value delivered", re.compile(r"\bvalue\s+delivered\b", re.I)),
    ("optimized / optimal", re.compile(r"\boptimi[sz]ed\b|\boptimal\b|\bbest\s+schedule\b", re.I)),
    ("typical restaurant", re.compile(r"\btypical\s+restaurants?\b", re.I)),
    ("claw back", re.compile(r"\bclaw(?:ed|s)?\s+back\b", re.I)),
    ("the one number", re.compile(r"\bthe\s+one\s+number\b", re.I)),
    ("AI reads", re.compile(r"\b(?:ChatGPT|Google\s+AI)\b[^.\n\"'<]{0,20}\b(?:reads?|uses|indexes)\b")),
    ("grow your score", re.compile(r"\bgrow\s+your\s+score\b", re.I)),
    ("not an estimate", re.compile(r"\bnot\s+an\s+estimate\b", re.I)),
    ("before they became problems", re.compile(r"\bbefore\s+(?:they|it)\s+became\s+(?:a\s+)?problems?\b", re.I)),
    ("will do better", re.compile(r"\bwill\s+do\s+better\b", re.I)),
    ("running on AI", re.compile(r"\brunning\s+on\s+AI\b", re.I)),
    ("band word as confidence", re.compile(r"\b(?:High|Medium|Low)\s+confidence\b")),
    ("savings label on a gap", re.compile(r"\b(?:Monthly|Annual|Yearly|Weekly)\s+savings\b|\bsavings\s+available\b|"
                                          r"\bSaving\s+vs\.?\s+industry\b|\bAnnual\s+advantage\b|\bLargest\s+saving\b|"
                                          r"\bIf\s+optimized\b|\bSaves\s+\$", re.I)),
    ("the promise", re.compile(r"\bTHE\s+PROMISE\b")),
]
ALLOW_MARKER = "owner-copy-ok"

# How many banned labels owner copy still carries. Ratchet only: lower it as
# copy is fixed (the lint says when); it fails the moment the count rises.
BASELINE = 45


# ── reading the text out of each kind of file ───────────────────────────────

def py_strings(source: str):
    """[(lineno, text, line)] for every string literal that is not a
    docstring. Comments are never tokens of type STRING."""
    out = []
    lines = source.splitlines()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    prev_sig = None
    for i, tok in enumerate(toks):
        kind = tok.type
        is_string = kind == tokenize.STRING or kind == getattr(tokenize, "FSTRING_MIDDLE", -1)
        if is_string:
            # A docstring is a string that is a whole statement: it follows
            # a NEWLINE/INDENT/DEDENT (or starts the file) and ends at NEWLINE.
            starts_stmt = prev_sig in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL)
            nxt = next((t for t in toks[i + 1:] if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
            if kind == tokenize.STRING and starts_stmt and nxt is not None and nxt.type in (
                    tokenize.NEWLINE, tokenize.ENDMARKER):
                prev_sig = kind
                continue
            line = lines[tok.start[0] - 1] if tok.start[0] - 1 < len(lines) else ""
            out.append((tok.start[0], tok.string, line))
        if kind not in (tokenize.COMMENT, tokenize.NL):
            prev_sig = kind
    return out


_SWIFT_STR_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def _strip_line_comment(line: str) -> str:
    """The line up to a // that is outside a string literal."""
    in_str, esc = False, False
    for i, ch in enumerate(line):
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str and line.startswith("//", i):
            return line[:i]
    return line


def swift_strings(source: str):
    out = []
    for n, line in enumerate(source.splitlines(), 1):
        code = _strip_line_comment(line)
        if code.lstrip().startswith(("/*", "*")):
            continue
        for m in _SWIFT_STR_RE.finditer(code):
            out.append((n, m.group(0), line))
    return out


def html_text(source: str):
    """[(lineno, line)] of an HTML/Jinja file with comments blanked."""
    body = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), source, flags=re.S)
    body = re.sub(r"\{#.*?#\}", lambda m: "\n" * m.group(0).count("\n"), body, flags=re.S)
    out = []
    in_block = False
    for n, line in enumerate(body.splitlines(), 1):
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            in_block = "*/" not in s
            continue
        if s.startswith(("//", "*")):
            continue
        out.append((n, line))
    return out


def _files():
    for name in PY_FILES:
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            yield name, "py"
    for d in HTML_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for f in files:
                if f.endswith((".html", ".htm", ".jinja", ".j2")):
                    yield os.path.relpath(os.path.join(dirpath, f), ROOT), "html"
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, SWIFT_DIR)):
        # Test targets are not owner copy: they often assert a label is ABSENT.
        if "/build" in dirpath or "DerivedData" in dirpath or re.search(r"Tests(?:/|$)", dirpath):
            continue
        for f in files:
            if f.endswith(".swift"):
                yield os.path.relpath(os.path.join(dirpath, f), ROOT), "swift"


def scan_source(source: str, kind: str):
    """[(lineno, label, matched)] banned labels in one file's owner text."""
    if kind == "py":
        units = [(n, text, line) for n, text, line in py_strings(source)]
    elif kind == "swift":
        units = swift_strings(source)
    else:
        units = [(n, line, line) for n, line in html_text(source)]
    hits = []
    for n, text, line in units:
        if ALLOW_MARKER in line:
            continue
        for label, pat in BANNED:
            for m in pat.finditer(text):
                hits.append((n, label, m.group(0)))
    return hits


def scan():
    out = []
    for rel, kind in _files():
        try:
            source = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for n, label, matched in scan_source(source, kind):
            out.append((rel, n, label, matched))
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    hits = scan()
    count = len(hits)
    by_file, by_label = {}, {}
    for rel, _n, label, _m in hits:
        by_file[rel] = by_file.get(rel, 0) + 1
        by_label[label] = by_label.get(label, 0) + 1
    if "--list" in argv:
        for rel, n, label, matched in hits:
            print(f"  {rel}:{n}  [{label}]  {matched!r}")
        print()
    if count:
        print("banned owner-copy labels by kind:")
        for label, c in sorted(by_label.items(), key=lambda kv: -kv[1]):
            print(f"  {c:>4}  {label}")
        print("top offenders:")
        for rel, c in sorted(by_file.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {c:>4}  {rel}")
        print()
    if count > BASELINE:
        print(f"{count} banned owner-copy labels — {count - BASELINE} more than the {BASELINE} already here.")
        print("Write what the figure is: 'gap to target', 'available, not captured', 'Measured results',")
        print("'Draft', a percentage instead of a band word (NS1 V7, NS3 R9). Run with --list to see each.")
        return 1
    if count < BASELINE:
        print(f"owner copy lint OK — {count} banned labels, down from {BASELINE}. "
              f"Lower BASELINE in this file to {count} to hold the gain.")
        return 0
    print(f"owner copy lint OK — holding at {count} banned labels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
