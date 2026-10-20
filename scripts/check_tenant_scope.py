"""Unscoped writes on tenant tables, held as a ratchet (scale audit 10/19/26,
SEC-16).

Isolation here is the app's own WHERE clauses: a helper checks that a row
is the session's restaurant's and then writes it by primary key. Every one
of the ~400 `UPDATE <t> ... WHERE id=?` / `DELETE FROM <t> WHERE id=?`
statements on a table with a `restaurant_id` column that does not also name
`restaurant_id` follows such a check today (A7 traced 45 of them: no IDOR).
Nothing stopped the next one from skipping the check.

This counts them per (file, table) and compares with
`scripts/tenant_scope_baseline.json`. A NEW unscoped write fails
(`tests/test_tenant_scope.py`): scope it (`AND restaurant_id=?`), or — when
the ownership check is genuinely just above it — raise the count in the
baseline in the same commit and say which check covers it. A count that
falls is lowered with `--update`. The window is the statement's own line and
the two after it (one SQL string across Python string concatenation), up
to the quote that closes it.

    python3 scripts/check_tenant_scope.py            # compare, exit 1 on a new one
    python3 scripts/check_tenant_scope.py --update   # rewrite the baseline (after review)

Reads too (blind re-audit 10/20/26 R7-18): a new `SELECT ... FROM <tenant
table> ... WHERE id=?` that does not name restaurant_id is counted the same
way against `scripts/tenant_scope_reads_baseline.json` (125 when it began;
the audit traced none that cross
tenants). At 1,500 tenants a new read helper called with a request-supplied
id is a cross-tenant leak no other test catches; Postgres row-level security
is the stronger guard, not on yet.

    python3 scripts/check_tenant_scope.py --update-reads   # rewrite the reads baseline
"""
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
_STMT = re.compile(r"\b(UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)
_BY_ID = re.compile(r"WHERE\s+(?:[a-z_]+\.)?id\s*=\s*\?", re.I)
# Where a statement's SQL string ends: a closing quote, then its arguments
# or the call's close (so the next statement on the next line is not read
# as part of this one).
_END = re.compile(r"[\"']\s*(?:,|\))")


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


def scan(tables=None):
    tables = tables if tables is not None else tenant_tables()
    counts = {}
    for path in _sources():
        lines = open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            for m in _STMT.finditer(line):
                table = m.group(2).lower()
                if table not in tables:
                    continue
                window = " ".join(lines[i:i + 3])
                tail = window[window.find(m.group(0)):]
                end = _END.search(tail)
                tail = tail[:end.start()] if end else tail
                if not _BY_ID.search(tail) or "restaurant_id" in tail:
                    continue
                key = f"{path}::{table}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def scan_reads(tables=None):
    """{"file::table": n} for SELECTs from a tenant table by primary key that
    do not name restaurant_id (the reads half of the ratchet, R7-18)."""
    tables = tables if tables is not None else tenant_tables()
    counts = {}
    for path in _sources():
        lines = open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            for m in _FROM.finditer(line):
                table = m.group(1).lower()
                if table not in tables or _DELETE_BEFORE.search(line[:m.start()]):
                    continue
                window = " ".join(lines[i:i + 3])
                tail = window[window.find(m.group(0)):]
                end = _END.search(tail)
                tail = tail[:end.start()] if end else tail
                if not _BY_ID.search(tail) or "restaurant_id" in tail:
                    continue
                key = f"{path}::{table}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def compare(counts, baseline):
    """[(key, now, allowed)] for every key over its baseline."""
    return [(k, n, baseline.get(k, 0)) for k, n in sorted(counts.items()) if n > baseline.get(k, 0)]


def main(argv):
    if "--update-reads" in argv:
        reads = scan_reads()
        with open(READS_BASELINE, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(reads.items())), fh, indent=1)
            fh.write("\n")
        print(f"reads baseline written: {sum(reads.values())} unscoped id reads in {len(reads)} places")
        return 0
    counts = scan()
    if "--update" in argv:
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(counts.items())), fh, indent=1)
            fh.write("\n")
        print(f"baseline written: {sum(counts.values())} unscoped writes in {len(counts)} places")
        return 0
    baseline = json.load(open(BASELINE, encoding="utf-8"))
    over = compare(counts, baseline)
    for k, n, allowed in over:
        print(f"NEW unscoped write: {k} has {n} (baseline {allowed}) — add AND restaurant_id=?, "
              "or raise the baseline naming the ownership check above it")
    reads = scan_reads()
    over_reads = compare(reads, json.load(open(READS_BASELINE, encoding="utf-8")))
    for k, n, allowed in over_reads:
        print(f"NEW unscoped read: {k} has {n} (baseline {allowed}) — add AND restaurant_id=?, "
              "or raise the reads baseline naming the ownership check above it")
    if over or over_reads:
        return 1
    print(f"tenant scope ratchet OK — {sum(counts.values())} unscoped id writes and "
          f"{sum(reads.values())} reads, none new")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
