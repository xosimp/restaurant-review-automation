"""Ratchets on the test suite itself (audit #25 A01-03, A01-11, A04-05, A04-10,
A07-16; TESTING.md, "Rules"). Each count can only go down.

1. The wall clock. A test that builds "today" from the PROCESS clock
   (`date.today()`, `datetime.now(...)`, `utcnow()`) while the code uses the
   restaurant's own date fails whenever the two disagree — every evening on a
   UTC runner, all day at UTC+14 — and kept CI red for weeks. New tests take
   time from `tests/clock.py` (restaurant_today, today_for, frozen).
   An alias is a read too: `from datetime import datetime as dt` then
   `dt.now()`, `from datetime import date as D` then `D.today()` (blind
   re-audit 10/27/26 B1 F14: nine such reads hid from the pattern).
2. Source text. A test that asserts on `inspect.getsource(...)` passes when a
   behaviour breaks but the string survives, and fails on a harmless rename.
   New tests drive the behaviour (run the job twice -> one send; a burst ->
   429). The count of call sites, per file, only goes down. So does the
   count of app modules read as TEXT another way: `open("…/client_api.py")`,
   `Path(…, "models.py").read_text()`, or a helper of the file's own that
   opens the name it is given (`_src("mobile_api.py")`).

Counts are of code only (comments and strings are ignored), per file, against
the baselines in tests/ratchets/. A file whose count dropped passes; lower its
baseline with:

    python tests/test_test_ratchets.py --update
"""
import io
import json
import os
import re
import sys
import tokenize

import pytest

pytestmark = pytest.mark.contract

TESTS = os.path.dirname(os.path.abspath(__file__))
RATCHETS = os.path.join(TESTS, "ratchets")

# The helpers that are the sanctioned way to read the clock, and this file
# (whose patterns name what they count).
EXEMPT = {"clock.py", "test_test_ratchets.py"}

WALL_CLOCK = re.compile(r"\b\w*date\s*\.\s*today\s*\(|\bdatetime\s*\.\s*(?:now|today)\s*\(|\butcnow\s*\(")
GETSOURCE = re.compile(r"\bgetsource\s*\(")
# `from datetime import datetime as dt` / `from datetime import date as D`.
_CLOCK_ALIAS = re.compile(r"\bfrom\s+datetime\s+import\s+([^\n;]+)")
ROOT = os.path.dirname(TESTS)


def _code_only(text):
    """The file's code tokens, space-joined: comments and string literals
    dropped (f-string expressions are tokens of their own and stay)."""
    out = []
    skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENCODING}
    skip |= {getattr(tokenize, n) for n in ("FSTRING_MIDDLE",) if hasattr(tokenize, n)}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type not in skip:
                out.append(tok.string)
    except (tokenize.TokenError, SyntaxError):
        return text
    return " ".join(out)


def _files():
    for dirpath, dirnames, filenames in os.walk(TESTS):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures", "ratchets")]
        for name in filenames:
            if name.endswith(".py") and name not in EXEMPT:
                path = os.path.join(dirpath, name)
                yield os.path.relpath(path, TESTS), path


def clock_reads(text):
    """Process-clock reads in a file's code, aliases of the datetime classes
    included."""
    code = _code_only(text)
    n = len(WALL_CLOCK.findall(code))
    for m in _CLOCK_ALIAS.finditer(text):
        names = m.group(1).replace("(", " ").replace(")", " ").replace("\\", " ")
        for part in names.split(","):
            bits = part.split()
            if len(bits) == 3 and bits[1] == "as" and bits[0] in ("datetime", "date") and bits[2] != bits[0]:
                alias, cls = bits[2], bits[0]
                # utcnow( is counted above whatever it is called on; an
                # attribute (self.dt.now) is not the alias.
                meth = "(?:now|today)" if cls == "datetime" else "today"
                n += len(re.findall(r"(?<![\w.])(?<!\. )" + re.escape(alias) + r"\s*\.\s*" + meth + r"\s*\(", code))
    return n


def _app_module(literal):
    """Whether a string constant names one of the app's own .py files (not
    a test, not a script that is run rather than read)."""
    if not (isinstance(literal, str) and literal.endswith(".py")):
        return False
    name = literal.replace("\\", "/").lstrip("./")
    if name.startswith(("tests/", "scripts/")) or os.path.basename(name).startswith(("test_", "conftest")):
        return False
    return os.path.exists(os.path.join(ROOT, name))


def source_reads(text):
    """Reads of app modules as text other than getsource: open(<…x.py…>),
    <…x.py…>.read_text(), and calls to a helper of this file that opens or
    read_text()s the path it is given, with an app module's name."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0

    def names_module(node):
        return any(isinstance(c, ast.Constant) and _app_module(c.value) for c in ast.walk(node))

    def is_read(call):
        f = call.func
        return (isinstance(f, ast.Name) and f.id == "open") or \
            (isinstance(f, ast.Attribute) and f.attr in ("read_text", "read_bytes"))

    readers = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(isinstance(c, ast.Call) and is_read(c) and not names_module(c) for c in ast.walk(fn))}
    n = 0
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if is_read(call):
            target = call if isinstance(call.func, ast.Name) else call.func.value
            n += names_module(target)
        elif isinstance(call.func, ast.Name) and call.func.id in readers:
            n += any(isinstance(a, ast.Constant) and _app_module(a.value) for a in call.args)
    return n


def scan(pattern):
    """{test file: count} for a regex over the code, or a counting function
    over the whole text."""
    counts = {}
    for rel, path in _files():
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        n = pattern(text) if callable(pattern) else len(pattern.findall(_code_only(text)))
        if n:
            counts[rel] = n
    return counts


def _baseline(name):
    with open(os.path.join(RATCHETS, name), encoding="utf-8") as fh:
        return json.load(fh)["counts"]


def _grown(now, base):
    return sorted((f, base.get(f, 0), n) for f, n in now.items() if n > base.get(f, 0))


def test_no_new_wall_clock_reads_in_tests():
    grown = _grown(scan(clock_reads), _baseline("wall_clock.json"))
    assert not grown, (
        "These test files read the process clock more often than the baseline allows "
        "(file, baseline, now): %s. Take time the way the code does — tests/clock.py: restaurant_today(), "
        "today_for(rid), this_monday(), utc_now(), or frozen(...) for a fixed instant." % grown)


def test_no_new_source_text_assertions_in_tests():
    grown = _grown(scan(GETSOURCE), _baseline("getsource.json"))
    assert not grown, (
        "These test files call inspect.getsource more often than the baseline allows (file, baseline, now): "
        "%s. Test the behaviour instead — run the job and count the sends, make the requests and read the "
        "status — never the source text." % grown)


def test_no_new_source_file_reads_in_tests():
    grown = _grown(scan(source_reads), _baseline("source_reads.json"))
    assert not grown, (
        "These test files read app modules as text (open / read_text / a reading helper) more often than the "
        "baseline allows (file, baseline, now): %s. Test the behaviour, not the source." % grown)


def test_a_clock_alias_and_a_source_read_are_counted():
    src = ("from datetime import datetime as dt, date as D, timedelta\n"
           "from datetime import (datetime as _when)\n"
           "a = dt.now()\nb = D.today()\nc = _when.utcnow()\nd = self.dt.now()\n"
           "e = datetime.now()\n")
    assert clock_reads(src) == 4          # dt.now, D.today, _when.utcnow, datetime.now — not self.dt.now
    reads = ("import os\nfrom pathlib import Path\n"
             "def _src(name):\n    with open(os.path.join(ROOT, name)) as fh:\n        return fh.read()\n"
             "def test_x():\n    a = open(os.path.join(ROOT, 'client_api.py')).read()\n"
             "    b = Path(ROOT, 'models.py').read_text()\n    c = _src('mobile_api.py')\n"
             "    d = open(os.path.join(ROOT, 'templates', 'dashboard.html')).read()\n"
             "    e = _src('tests/test_models.py')\n")
    assert source_reads(reads) == 3       # the template and the test file are not app modules


def test_the_scanner_counts_code_and_ignores_comments_and_strings():
    src = ('import datetime as dt\n'
           'x = dt.date.today()  # date.today() in a comment\n'
           's = "datetime.now() in a string"\n'
           'y = f"{datetime.now(tz)}"\n'
           'z = datetime.datetime.utcnow()\n'
           'w = inspect.getsource(f)\n')
    code = _code_only(src)
    assert len(WALL_CLOCK.findall(code)) == 3
    assert len(GETSOURCE.findall(code)) == 1
    # A helper that only resembles one is not counted.
    assert not WALL_CLOCK.findall(_code_only("t = time_utils.restaurant_now(tz)\nd = restaurant_today()\n"))


def _update():
    os.makedirs(RATCHETS, exist_ok=True)
    for name, pattern, about in (
            ("wall_clock.json", clock_reads,
             "date.today() / datetime.now( / datetime.today( / utcnow( per test file, aliases included"),
            ("getsource.json", GETSOURCE, "inspect.getsource( call sites per test file"),
            ("source_reads.json", source_reads,
             "app modules read as text by open( / read_text( / a reading helper, per test file")):
        counts = scan(pattern)
        with open(os.path.join(RATCHETS, name), "w", encoding="utf-8") as fh:
            json.dump({"about": about + " (tests/test_test_ratchets.py; only goes down)",
                       "total": sum(counts.values()), "files": len(counts),
                       "counts": dict(sorted(counts.items()))}, fh, indent=1)
            fh.write("\n")
        print(f"{name}: {sum(counts.values())} in {len(counts)} files")


if __name__ == "__main__":
    if "--update" in sys.argv:
        _update()
    else:
        print(__doc__)
