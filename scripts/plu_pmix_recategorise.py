"""scripts/plu_pmix_recategorise.py — re-file Back Office PLU rows under the
category their P-Mix line actually sits under (bar audit 10/14/26 V41 /
RPW-14: the 10/5/26 import filed each line under the header BELOW it, so 102
beers read "White Wine"). Run by hand on production (railway ssh) with the
text of Back Office's Consolidated Product Mix (the PDF the 10/5 import read,
e.g. `pdftotext -layout -f 29 -l 53 pmix.pdf pmix.txt`), dry run first:

    python3 scripts/plu_pmix_recategorise.py --rid 5 --text pmix.txt
    python3 scripts/plu_pmix_recategorise.py --rid 5 --text pmix.txt --apply

A manual row is never touched; a row the P-Mix doesn't list is left as it
is. Idempotent. Prints JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rid", type=int, required=True)
    ap.add_argument("--text", required=True, help="the P-Mix as text")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a = ap.parse_args(argv)
    import plu_map
    text = open(a.text, encoding="utf-8", errors="replace").read()
    parsed = plu_map.parse_pmix(text)
    out = {"rid": a.rid, "applied": bool(a.apply), "lines": len(parsed),
           "categories": sorted({str(r["category"]) for r in parsed})}
    if a.apply:
        out["result"] = plu_map.recategorise(a.rid, text)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
