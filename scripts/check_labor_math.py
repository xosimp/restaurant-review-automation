"""Labor-cost-over-sales arithmetic outside the modules that own it, held as a
ratchet (audit #25 F2 / R7).

Labor % had fifteen independent implementations, each deciding for itself
whether the salaries were in it: the stored `labor_daily_history.labor_pct`
is HOURLY, the owner's labor % is ALL-IN (salaries included, 9/30/26), a
manager's must stay hourly. Three salary-view drifts shipped in one month.
The figure belongs to `metrics.labor_figures` (and the analysis in
`labor.py`, the nightly report in `dsr/`); this counts, per file, every line
of code anywhere else that divides a labor figure by a sales figure (or
weights a labor % by sales), and compares the counts with
`scripts/labor_math_baseline.json`, where each grandfathered file carries the
reason its arithmetic is not an owner-facing labor % (hourly by design, a
schedule budget, a variance, …).

A NEW site fails (`tests/test_labor_pct_one_figure.py`): read the figure
through `metrics.labor_figures` / `metrics.daily_labor`, or — when the
arithmetic is genuinely not a labor % shown to anyone (a budget, a spread)
— raise the file's count in the baseline in the same commit and say why.
Comments and docstrings are not code and are not counted. A count that
falls is lowered with `--update` (which keeps each file's reason).

A second ratchet (blind re-audit 10/26/26 B3): every SQL statement outside
the owners that SELECTs the STORED `labor_pct` from `labor_history` or
`labor_daily_history`. That column is hourly; read into owner-facing text it
is the drift class that shipped three times (the labor-over alert compared
it with the all-in target, the weekly digest set it beside the all-in
headline). Counted per file against `scripts/labor_stored_pct_baseline.json`,
each grandfathered file with its reason; a new reader goes through
`models.get_labor_history` / `metrics.labor_figures` instead.

    python3 scripts/check_labor_math.py            # compare, exit 1 on a new site
    python3 scripts/check_labor_math.py --update   # rewrite the counts (after review)
"""
import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "scripts", "labor_math_baseline.json")
STORED_BASELINE = os.path.join(ROOT, "scripts", "labor_stored_pct_baseline.json")
# One SQL statement that selects the stored labor_pct from a labor history
# table (labor_pct named between SELECT and the FROM of that table).
STORED_PCT = re.compile(r"SELECT\b(?:(?!\bFROM\b).)*?\blabor_pct\b(?:(?!\bFROM\b).)*?"
                        r"\bFROM\s+labor_(?:daily_)?history\b", re.I | re.S)

# The modules that own the figure: never counted.
OWNERS = ("metrics.py", "labor.py")
OWNER_DIRS = ("dsr/",)

# A labor-named value divided by a sales-named one on one line, a labor %
# weighted by sales, or SQL's "labor_cost * 100.0 / sales".
_LABOR = r"\b\w*labor\w*\b"
_SALES = r"\b(?:\w*sales\w*|net|net_\w+|\w+_net)\b"
PATTERNS = (
    re.compile(_LABOR + r"[^/\n]{0,80}?/\s*[^/\n]{0,60}?" + _SALES, re.I),
    re.compile(r"\blabor_pct\b[\"'\]\)]*\s*\*\s*(?:float\()?[\w\.\[\]\"']{0,20}?" + _SALES, re.I),
    re.compile(r"\b\w*labor_cost\w*\b\)?\s*\*\s*100(?:\.0)?\s*/", re.I),
)


def _sources():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    return [f for f in files if not f.startswith(("tests/", "scripts/", ".claude/")) and f not in OWNERS
            and not f.startswith(OWNER_DIRS)]


def _docstring_lines(tree) -> set:
    """Line numbers inside docstrings and bare string statements."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                and isinstance(node.value.value, str):
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def code_lines(text) -> list:
    """(line number, the line with comments removed) for every line that is
    code — docstrings and comment-only lines dropped."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    skip = _docstring_lines(tree)
    lines = text.splitlines()
    comments = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.start[1]
    except (tokenize.TokenError, IndentationError):
        pass
    out = []
    for i, line in enumerate(lines, start=1):
        if i in skip:
            continue
        if i in comments:
            line = line[:comments[i]]
        if line.strip():
            out.append((i, line))
    return out


def count_text(text) -> int:
    return sum(1 for _, line in code_lines(text) if any(p.search(line) for p in PATTERNS))


def _string_literals(tree):
    """Every string literal's text, an f-string read whole (its pieces
    joined, each placeholder a space) and never again piece by piece."""
    out = []

    def walk(node):
        if isinstance(node, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " "
                               for v in node.values))
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)
    walk(tree)
    return out


def count_stored_reads(text) -> int:
    """SQL statements in `text` that select the stored (hourly) labor_pct
    from labor_history / labor_daily_history (STORED_PCT)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    return sum(len(STORED_PCT.findall(s)) for s in _string_literals(tree))


def scan_stored() -> dict:
    """{file: n} — stored-labor_pct reads in each file outside the owners."""
    counts = {}
    for path in _sources():
        try:
            text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        n = count_stored_reads(text)
        if n:
            counts[path] = n
    return counts


def scan() -> dict:
    """{file: n} — the labor ÷ sales lines in each file outside the owners."""
    counts = {}
    for path in _sources():
        try:
            text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        n = count_text(text)
        if n:
            counts[path] = n
    return counts


def load_baseline(path=None) -> dict:
    """{file: {"n": int, "why": str}}."""
    try:
        return json.load(open(path or BASELINE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def compare(counts, baseline) -> list:
    """[(file, now, allowed)] for every file over its baseline."""
    return [(f, n, int((baseline.get(f) or {}).get("n", 0))) for f, n in sorted(counts.items())
            if n > int((baseline.get(f) or {}).get("n", 0))]


def main(argv):
    counts = scan()
    baseline = load_baseline()
    stored = scan_stored()
    stored_base = load_baseline(STORED_BASELINE)
    if "--update" in argv:
        for path, got, base, what in ((BASELINE, counts, baseline, "labor/sales lines"),
                                      (STORED_BASELINE, stored, stored_base, "stored labor_pct reads")):
            out = {f: {"n": n, "why": (base.get(f) or {}).get("why") or "TODO: say why this is not an owner-facing "
                                                                         "labor % (or route it through metrics)"}
                   for f, n in sorted(got.items())}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=1)
                fh.write("\n")
            print(f"baseline written: {sum(got.values())} {what} in {len(got)} files")
        return 0
    over = compare(counts, baseline)
    for f, n, allowed in over:
        print(f"{f}: {n} labor/sales arithmetic line(s), baseline {allowed} — read it through "
              "metrics.labor_figures, or raise the baseline with the reason")
    over_stored = compare(stored, stored_base)
    for f, n, allowed in over_stored:
        print(f"{f}: {n} SQL read(s) of the stored HOURLY labor_pct, baseline {allowed} — read it through "
              "models.get_labor_history / metrics.labor_figures, or raise the baseline with the reason")
    return 1 if (over or over_stored) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
