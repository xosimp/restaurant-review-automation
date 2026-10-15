"""The scheduling code's comments and docstrings say what the code does, not
which audit finding asked for it.

They used to carry the ledger — "(SCALE-24 keeps P-41)", "LEARNING-12,
LEARNING-24:", "blind re-audit 10/11/26 STRESS-14" — about 1,900 citations
in comments and 1,400 in strings across the schedule modules, which told a
reader where a line came from but not what it did. The finding IDs live in the
audit registers (docs/audits/) and the tests that cite them; the code says the
behaviour. This holds every schedule module (and labor, owner_rules,
shift_quality) at zero, in comments and in docstrings. Log text and data
strings (the benchmark's archetype provenance) are not docstrings and are not
read here.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# An audit finding ID: a register prefix, a dash, a number ("SCALE-24",
# "LEARNING-12", "P-41", "DH1-10").
AUDIT_ID = re.compile(
    r"\b(?:PR|P|D|SQ|ARCH|LOGIC|BUGSGEN|AI|RM|LEARN|LEARNING|E|L|DH\d|BM\d|STRESS|SCALE|TRUST|PROMPT|PIPE"
    r"|SCHED|DATA|RULES|MARKET|INT|UI|SCHEDULE|PLATFORM|ROUTE|OPS|WEB|IOS|FIX|NS\d|CLEAN|BUGS|STAFF|REC|MEM"
    r"|C|J|K|M|R|S|T|W)-\d{1,3}\b")


def _modules():
    files = sorted(ROOT.glob("schedule_*.py"))
    files += [ROOT / "labor.py", ROOT / "owner_rules.py", ROOT / "shift_quality.py"]
    return files


def _comment_hits(src):
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and AUDIT_ID.search(tok.string):
            out.append((tok.start[0], tok.string.strip()))
    return out


def _docstring_hits(src):
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and AUDIT_ID.search(doc):
                out.append((getattr(node, "lineno", 1), AUDIT_ID.findall(doc)))
    return out


def test_the_modules_are_found():
    names = {p.name for p in _modules()}
    assert {"schedule_engine.py", "schedule_job.py", "labor.py", "shift_quality.py"} <= names
    assert all(p.exists() for p in _modules())


def test_no_audit_ids_in_schedule_comments():
    hits = {p.name: _comment_hits(p.read_text()) for p in _modules()}
    hits = {k: v for k, v in hits.items() if v}
    assert not hits, f"say what the line does, not which finding asked for it: {hits}"


def test_no_audit_ids_in_schedule_docstrings():
    hits = {p.name: _docstring_hits(p.read_text()) for p in _modules()}
    hits = {k: v for k, v in hits.items() if v}
    assert not hits, f"say what the code does, not which finding asked for it: {hits}"


def test_the_pattern_reads_a_citation():
    # the check is not vacuous: it reads the forms the ledger used
    assert _comment_hits("x = 1  # (SCALE-24 keeps P-41)\n")
    assert _comment_hits("# blind re-audit 10/11/26 STRESS-14\n")
    assert _docstring_hits('def f():\n    """The pattern side of LEARN-5."""\n')
    assert not _comment_hits("# the 4-4-5 calendar, a 16-17 year old, Server AM\n")
