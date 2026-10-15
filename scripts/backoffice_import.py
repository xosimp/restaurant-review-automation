"""scripts/backoffice_import.py — read a Back Office file into a restaurant
(backoffice_ap.read_file): the Chart of Accounts CSV (Report Center → Export
Chart of Accounts), the Invoices By Vendor page printed to PDF, or one
invoice's Edit Invoice page printed to PDF (its line items, each with its GL
account). Dry run by default (parses and reports, writes nothing); --apply
stores it. `--file -`
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
        elif bo.looks_like_invoice_page(text := bo._text_of(name, content)):
            inv = bo.parse_invoice_page(text)
            from collections import Counter
            by_gl = Counter()
            for l in inv["lines"]:
                by_gl[l["gl"]] += l["ext_amount"]
            out = {"kind": "invoice_page", "vendor": inv["vendor"], "invoice_number": inv["invoice_number"],
                   "date": inv["date"], "total": inv["total"], "lines": len(inv["lines"]), "adds_up": inv["adds_up"],
                   "by_gl": {k: round(v, 2) for k, v in sorted(by_gl.items(), key=lambda x: str(x[0]))}}
        else:
            parsed = bo.parse_invoices_by_vendor(text)
            out = {"kind": "invoices_by_vendor", "vendors": len(parsed["vendors"]),
                   "entries": sum(len(v["rows"]) for v in parsed["vendors"]),
                   "unparsed": parsed["unparsed"], "mismatched": parsed["mismatched"]}
    out["applied"] = bool(a.apply)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
