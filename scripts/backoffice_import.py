"""scripts/backoffice_import.py — read a Back Office file into a restaurant
(backoffice_ap.read_file): the Chart of Accounts CSV (Report Center → Export
Chart of Accounts), the Invoices By Vendor page printed to PDF, or one
invoice's Edit Invoice page printed to PDF (its line items, each with its GL
account), or the restaurant's recipe book read from Back Office's recipe
and item pages (JSON: backoffice_recipes.load, e.g.
docs/clients/simple-ejs/back_office_recipes.json), or submitted inventory
counts ({"inventory_counts": …}, from scripts/backoffice_read_counts.js run in
a signed-in Back Office tab; backoffice_counts.save_counts). Dry run by default (parses and reports, writes nothing); --apply
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
    ap.add_argument("--vendors", default=None, help="an invoice book: only these vendors, |-separated")
    a = ap.parse_args(argv)
    content = sys.stdin.buffer.read() if a.file == "-" else open(a.file, "rb").read()
    name = a.name or os.path.basename(a.file)
    import backoffice_ap as bo
    if b'"inventory_counts"' in content[:200]:
        import backoffice_counts as bc
        b = json.loads(content.decode("utf-8-sig"))
        if a.apply:
            out = dict(bc.save_counts(a.rid, b), kind="inventory_counts")
        else:
            ps = b.get("inventory_counts") or []
            out = {"kind": "inventory_counts", "periods": [
                {"end": p.get("end"), "status": p.get("status"), "accounts": len(p.get("gls") or []),
                 "items": len(p.get("items") or []),
                 "food": round(sum(float(g.get("amount") or 0) for g in p.get("gls") or []
                                   if bc.category(g.get("name")) == "food"), 2)} for p in ps]}
        out["applied"] = bool(a.apply)
        print(json.dumps(out, indent=1, default=str))
        return 0
    if name.lower().endswith(".json") and b'"invoices"' in content[:200]:
        b = json.loads(content.decode("utf-8-sig"))
        vendors = [v for v in (a.vendors or "").split("|") if v.strip()] or None
        if a.apply:
            out = dict(bo.save_invoice_book(a.rid, b, vendors=vendors), kind="invoice_book")
        else:
            sel = [i for i in b.get("invoices") or []
                   if not vendors or str(i.get("vendor") or "").strip().upper() in {v.strip().upper() for v in vendors}]
            out = {"kind": "invoice_book", "invoices": len(sel), "lines": sum(len(i.get("items") or []) for i in sel)}
        out["applied"] = bool(a.apply)
        print(json.dumps(out, indent=1, default=str))
        return 0
    if name.lower().endswith(".json"):
        import backoffice_recipes as br
        b = json.loads(content.decode("utf-8-sig"))
        if a.apply:
            out = dict(br.load(a.rid, b, source=b.get("source")), kind="recipe_book")
        else:
            br.validate(b)
            out = {"kind": "recipe_book", "recipes": len(b["recipes"]), "items": len(b["items"]),
                   "mapped": len(b.get("pos_names") or {})}
        out["applied"] = bool(a.apply)
        print(json.dumps(out, indent=1, default=str))
        return 0
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
            # What --apply would do: vendors whose rows don't add up are held
            # (not saved), and the printout is the record over its dates —
            # entries on file there that it no longer shows would be removed
            # (into bo_invoice_removals). Nothing is written here.
            parsed = bo.parse_invoices_by_vendor(text)
            pv = bo.preview_printout(a.rid, parsed)
            out = {"kind": "invoices_by_vendor", "vendors": len(parsed["vendors"]),
                   "entries": sum(len(v["rows"]) for v in parsed["vendors"]),
                   "unparsed": parsed["unparsed"], "orphans": parsed.get("orphans", 0),
                   "mismatched": parsed["mismatched"], "held": pv["held"], "would_remove": pv["would_remove"]}
    out["applied"] = bool(a.apply)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
