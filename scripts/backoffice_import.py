"""scripts/backoffice_import.py — read a Back Office file into a restaurant
(backoffice_ap.read_file): the Chart of Accounts CSV (Report Center → Export
Chart of Accounts) or the Invoices By Vendor page printed to PDF. Dry run by
default (parses and reports, writes nothing); --apply stores it. `--file -`
reads the file from stdin, so a file on a laptop reaches production with

    railway ssh "cd /app && /opt/venv/bin/python scripts/backoffice_import.py --rid 5 --name coa.csv --file - --apply" < COAExport.csv

Sends nothing, calls no model, reads no provider. Prints JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rid", type=int, required=True)
    ap.add_argument("--file", required=True, help="a path, or - for stdin")
    ap.add_argument("--name", default=None, help="the file's name (with --file -)")
    ap.add_argument("--apply", action="store_true", help="store it (default: dry run)")
    a = ap.parse_args(argv)
    content = sys.stdin.buffer.read() if a.file == "-" else open(a.file, "rb").read()
    name = a.name or os.path.basename(a.file)
    import backoffice_ap as bo
    if a.apply:
        out = bo.read_file(a.rid, name, content)
    else:
        head = content[:400].decode("utf-8-sig", "replace")
        if "GLAccountSubTypeName" in head:
            accts = bo.parse_coa(content)
            from collections import Counter
            out = {"kind": "coa", "accounts": len(accts),
                   "categories": dict(Counter(x["category"] for x in accts))}
        else:
            parsed = bo.parse_invoices_by_vendor(bo._text_of(name, content))
            out = {"kind": "invoices_by_vendor", "vendors": len(parsed["vendors"]),
                   "entries": sum(len(v["rows"]) for v in parsed["vendors"]),
                   "unparsed": parsed["unparsed"], "mismatched": parsed["mismatched"]}
    out["applied"] = bool(a.apply)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
