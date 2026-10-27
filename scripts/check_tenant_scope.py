"""Unscoped writes and reads on tenant tables, held as a ratchet (scale audit
10/19/26, SEC-16; widened by audit #25 A03-06, 10/26/26).

Isolation here is the app's own WHERE clauses: a helper checks that a row
is the session's restaurant's and then writes it by primary key. Every
`UPDATE <t> ... WHERE ... id=?` / `DELETE FROM <t> WHERE ... id IN (...)`
statement on a table with a `restaurant_id` column that does not also name
`restaurant_id` must follow such a check. Nothing generic stops the next one
from skipping it, so this counts them per (file, table) and compares with
`scripts/tenant_scope_baseline.json`. A NEW unscoped statement fails
(`tests/test_tenant_scope.py`): scope it (`AND restaurant_id=?`), or — when
the ownership check is genuinely just above it — raise the count in the
baseline in the same commit and say in its "why" which check covers it.

What counts (A03-06 widened it):
  - every SQL string the code builds: a literal, an implicit concatenation,
    a triple-quoted block of any length, an f-string (its holes read as
    `{X}`, so `WHERE id IN ({ph})` counts), and `+` joins — read from the
    AST, so the statement is read to its end, not a 3-line window;
  - `id = ?` or `id IN (` ANYWHERE after the statement's WHERE, optionally
    table-qualified (`r.id = ?`), so `WHERE status=? AND id=?` counts.
    `review_id=?` / `restaurant_id=?` are other columns and do not;
  - scoped means `restaurant_id` appears in the statement (for an UPDATE,
    from its WHERE on — setting restaurant_id is not scoping by it).
Docstrings are not read; comments never were SQL.

Each baseline entry is {"file::table": {"n": N, "why": "..."}}: the count
and the ownership check (or the reason no tenant's id can reach it — an
admin-only route, a job's own rows) that covers every site. A count that
falls is lowered with --update, which keeps every "why" and writes "TODO"
for a new key; the test fails on an empty or TODO reason.

    python3 scripts/check_tenant_scope.py                # compare, exit 1 on a new one
    python3 scripts/check_tenant_scope.py --list         # every counted site, file:line
    python3 scripts/check_tenant_scope.py --update       # rewrite the writes baseline (after review)
    python3 scripts/check_tenant_scope.py --update-reads # rewrite the reads baseline (after review)

Reads too (blind re-audit 10/20/26 R7-18): a `SELECT ... FROM <tenant
table> ... WHERE ... id=?` that does not name restaurant_id is counted the
same way against `scripts/tenant_scope_reads_baseline.json`. At 1,500
tenants a new read helper called with a request-supplied id is a
cross-tenant leak; tests/test_cross_tenant_crawl.py walks every route with
another tenant's ids as the behavioural half of this guard.
"""
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "scripts", "tenant_scope_baseline.json")
READS_BASELINE = os.path.join(ROOT, "scripts", "tenant_scope_reads_baseline.json")
_FROM = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)
_DELETE_BEFORE = re.compile(r"DELETE\s*$", re.I)
_STMT = re.compile(r"\b(UPDATE(?:\s+OR\s+[A-Za-z]+)?|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)
_WHERE = re.compile(r"\bWHERE\b", re.I)
# `id = ?` / `id IN (` — bare or table-qualified (`r.id`), never the tail of
# another column (`review_id`, `restaurant_id`: `_` is a word character, so
# the look-behind refuses them).
_BY_ID = re.compile(r"(?:(?<![\w.])[A-Za-z_][A-Za-z0-9_]*\.|(?<![\w.]))id\s*(?:=\s*\?|IN\s*\()", re.I)
_SCOPED = re.compile(r"restaurant_id", re.I)
_HOLE = "{X}"


def tenant_tables():
    """Every table with a restaurant_id column, read from the CREATE TABLE
    statements in the code (no database needed)."""
    out = set()
    create = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*[\"';]",
                        re.S | re.I)
    alter = re.compile(r"ALTER TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD COLUMN\s+restaurant_id\b", re.I)
    for path in _sources():
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        for m in create.finditer(text):
            if re.search(r"\brestaurant_id\b", m.group(2)):
                out.add(m.group(1).lower())
        for m in alter.finditer(text):
            out.add(m.group(1).lower())
    out.discard("restaurants")
    return out


def _sources():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    return [f for f in files if not f.startswith(("tests/", "scripts/"))]


def _text(node):
    """The text a string expression builds — a literal, an f-string (holes
    as {X}), a `+` join or the left side of a `%` — or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _HOLE
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text(node.left), _text(node.right)
        if a is None and b is None:
            return None
        return (a if a is not None else _HOLE) + (b if b is not None else _HOLE)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _text(node.left)
    return None


def _strings(source):
    """[(line, text)] for every outermost string expression in `source` that
    could hold a statement (a bare string statement — a docstring — is left
    out)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                                   # a docstring or a bare string
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            text = _text(node)
            if text is not None:
                upper = text.upper()
                if "FROM" in upper or "UPDATE" in upper:
                    out.append((node.lineno, text))
                if isinstance(node, ast.BinOp):
                    # an operand that is not itself string text (a call
                    # that builds SQL of its own) is still read
                    stack.extend(c for c in (node.left, node.right) if _text(c) is None)
                continue
        stack.extend(ast.iter_child_nodes(node))
    return out


_CACHE = {}


def _file_strings(path):
    """_strings of one source file, kept per (path, mtime, size) — the
    writes and the reads scans read the same files."""
    full = os.path.join(ROOT, path)
    st = os.stat(full)
    key = (full, st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        with open(full, encoding="utf-8") as fh:
            _CACHE[key] = _strings(fh.read())
    return _CACHE[key]


def _unscoped(tail, scope_from_where=False):
    """True when the statement text `tail` (from its keyword to the end of
    the string) picks rows by id after its WHERE and never names
    restaurant_id."""
    tail = tail.split(";")[0]
    w = _WHERE.search(tail)
    if not w or not _BY_ID.search(tail, w.end()):
        return False
    return not _SCOPED.search(tail[w.start():] if scope_from_where else tail)


def _sites(tables, reads):
    """[(path, line, table)] for every counted statement."""
    out = []
    for path in _sources():
        for line, text in _file_strings(path):
            if reads:
                for m in _FROM.finditer(text):
                    table = m.group(1).lower()
                    if table not in tables or _DELETE_BEFORE.search(text[:m.start()]):
                        continue
                    if _unscoped(text[m.start():]):
                        out.append((path, line, table))
            else:
                for m in _STMT.finditer(text):
                    table = m.group(2).lower()
                    if table not in tables:
                        continue
                    if _unscoped(text[m.start():], scope_from_where=m.group(1).upper().startswith("UPDATE")):
                        out.append((path, line, table))
    return out


def _count(sites):
    counts = {}
    for path, _line, table in sites:
        key = f"{path}::{table}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def scan(tables=None):
    """{"file::table": n} for UPDATE/DELETE on a tenant table by id that do
    not name restaurant_id."""
    tables = tables if tables is not None else tenant_tables()
    return _count(_sites(tables, reads=False))


def scan_reads(tables=None):
    """{"file::table": n} for SELECTs from a tenant table by id that do not
    name restaurant_id (the reads half of the ratchet, R7-18)."""
    tables = tables if tables is not None else tenant_tables()
    return _count(_sites(tables, reads=True))


def allowed(entry):
    """A baseline entry's count: {"n": N, "why": ...} (or a bare int, the
    format before A03-06)."""
    if isinstance(entry, dict):
        return int(entry.get("n") or 0)
    return int(entry or 0)


def compare(counts, baseline):
    """[(key, now, allowed)] for every key over its baseline."""
    return [(k, n, allowed(baseline.get(k, 0))) for k, n in sorted(counts.items())
            if n > allowed(baseline.get(k, 0))]


def unexplained(baseline):
    """Keys whose "why" is missing, empty or TODO."""
    out = []
    for k, entry in sorted(baseline.items()):
        why = entry.get("why") if isinstance(entry, dict) else None
        if not why or not str(why).strip() or str(why).strip().upper().startswith("TODO"):
            out.append(k)
    return out


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _write(path, counts):
    """Rewrite a baseline to `counts`, keeping each surviving key's why."""
    old = _load(path)
    out = {}
    for k, n in sorted(counts.items()):
        why = old.get(k, {}).get("why") if isinstance(old.get(k), dict) else None
        out[k] = {"n": n, "why": why or "TODO"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    return out


def main(argv):
    if "--list" in argv:
        tables = tenant_tables()
        for kind, reads in (("write", False), ("read", True)):
            for path, line, table in _sites(tables, reads):
                print(f"{kind}\t{path}:{line}\t{table}")
        return 0
    if "--update-reads" in argv:
        reads = scan_reads()
        out = _write(READS_BASELINE, reads)
        print(f"reads baseline written: {sum(reads.values())} unscoped id reads in {len(reads)} places; "
              f"{len(unexplained(out))} need a why")
        return 0
    counts = scan()
    if "--update" in argv:
        out = _write(BASELINE, counts)
        print(f"baseline written: {sum(counts.values())} unscoped writes in {len(counts)} places; "
              f"{len(unexplained(out))} need a why")
        return 0
    baseline = _load(BASELINE)
    over = compare(counts, baseline)
    for k, n, allowed_n in over:
        print(f"NEW unscoped write: {k} has {n} (baseline {allowed_n}) — add AND restaurant_id=?, "
              "or raise the baseline naming the ownership check above it")
    reads = scan_reads()
    reads_baseline = _load(READS_BASELINE)
    over_reads = compare(reads, reads_baseline)
    for k, n, allowed_n in over_reads:
        print(f"NEW unscoped read: {k} has {n} (baseline {allowed_n}) — add AND restaurant_id=?, "
              "or raise the reads baseline naming the ownership check above it")
    missing = unexplained(baseline) + unexplained(reads_baseline)
    for k in missing:
        print(f"UNEXPLAINED baseline entry: {k} — say which ownership check covers it")
    if over or over_reads or missing:
        return 1
    print(f"tenant scope ratchet OK — {sum(counts.values())} unscoped id writes and "
          f"{sum(reads.values())} reads, none new, every one explained")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
