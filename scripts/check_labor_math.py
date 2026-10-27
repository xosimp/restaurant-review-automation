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


def load_baseline() -> dict:
    """{file: {"n": int, "why": str}}."""
    try:
        return json.load(open(BASELINE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def compare(counts, baseline) -> list:
    """[(file, now, allowed)] for every file over its baseline."""
    return [(f, n, int((baseline.get(f) or {}).get("n", 0))) for f, n in sorted(counts.items())
            if n > int((baseline.get(f) or {}).get("n", 0))]


def main(argv):
    counts = scan()
    baseline = load_baseline()
    if "--update" in argv:
        out = {f: {"n": n, "why": (baseline.get(f) or {}).get("why") or "TODO: say why this is not an owner-facing "
                                                                           "labor % (or route it through metrics)"}
               for f, n in sorted(counts.items())}
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
            fh.write("\n")
        print(f"baseline written: {sum(counts.values())} labor/sales lines in {len(counts)} files")
        return 0
    over = compare(counts, baseline)
    for f, n, allowed in over:
        print(f"{f}: {n} labor/sales arithmetic line(s), baseline {allowed} — read it through "
              "metrics.labor_figures, or raise the baseline with the reason")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
