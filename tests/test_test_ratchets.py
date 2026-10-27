"""Ratchets on the test suite itself (audit #25 A01-03, A01-11, A04-05, A04-10,
A07-16; TESTING.md, "Rules"). Each count can only go down.

1. The wall clock. A test that builds "today" from the PROCESS clock
   (`date.today()`, `datetime.now(...)`, `utcnow()`) while the code uses the
   restaurant's own date fails whenever the two disagree — every evening on a
   UTC runner, all day at UTC+14 — and kept CI red for weeks. New tests take
   time from `tests/clock.py` (restaurant_today, today_for, frozen).
2. Source text. A test that asserts on `inspect.getsource(...)` passes when a
   behaviour breaks but the string survives, and fails on a harmless rename.
   New tests drive the behaviour (run the job twice -> one send; a burst ->
   429). The count of call sites, per file, only goes down.

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


def scan(pattern):
    counts = {}
    for rel, path in _files():
        with open(path, encoding="utf-8") as fh:
            n = len(pattern.findall(_code_only(fh.read())))
        if n:
            counts[rel] = n
    return counts


def _baseline(name):
    with open(os.path.join(RATCHETS, name), encoding="utf-8") as fh:
        return json.load(fh)["counts"]


def _grown(now, base):
    return sorted((f, base.get(f, 0), n) for f, n in now.items() if n > base.get(f, 0))


def test_no_new_wall_clock_reads_in_tests():
    grown = _grown(scan(WALL_CLOCK), _baseline("wall_clock.json"))
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
            ("wall_clock.json", WALL_CLOCK, "date.today() / datetime.now( / datetime.today( / utcnow( per test file"),
            ("getsource.json", GETSOURCE, "inspect.getsource( call sites per test file")):
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
