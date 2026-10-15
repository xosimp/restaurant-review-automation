"""Re-read every stored Fintech file of a restaurant with today's reader.

The bar fix round of 10/15/26 changed what the reader writes from the same
files: the export's keg deposits ($30 a keg in Item Adjustments) and every
EMPTY / EMPTIES line as a deposit credit, printout lines taking their unit
and UPC from the export history, item codes without their padding,
canonical distributor names and invoice numbers (an invoice stored twice
under two spellings is merged), the Ref Inv / PO fields, and only the read
columns kept with an export's text. The files are kept, so this writes the
invoices again from them, oldest file first, through the same rules a new
file meets (a version that outranks a file's copy is never replaced). It
also teaches the restaurant's Fintech store map from its invoices when they
name one store, and (with --orphans) deletes invoice lines whose invoice is
gone.

Idempotent. Dry run by default: everything runs in one transaction that is
rolled back, and the before/after says what would change.

    python scripts/bar_reread_fintech.py --restaurant 5
    python scripts/bar_reread_fintech.py --restaurant 5 --apply
    python scripts/bar_reread_fintech.py --restaurant 5 --apply --orphans

On production: railway ssh "/opt/venv/bin/python scripts/bar_reread_fintech.py --restaurant 5"
(dry run first; the lead applies it).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="write the result (default: dry run)")
    ap.add_argument("--orphans", action="store_true", help="also count (and with --apply delete) orphaned lines")
    ap.add_argument("--db", default=None, help="a database file other than the platform's own")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    import fintech_invoices
    out = fintech_invoices.reread_all(a.restaurant, apply=a.apply, db_path=a.db)
    if a.orphans:
        out["orphan_lines"] = fintech_invoices.delete_orphan_lines(dry_run=not a.apply, db_path=a.db)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    b, f = out["before"], out["after"]
    print(f"Restaurant {out['restaurant_id']}: {len(out['files'])} stored files re-read, oldest first.")
    print(f"  invoices {b['invoices']} -> {f['invoices']} (balanced {b['balanced']} -> {f['balanced']}); "
          f"printout units inferred {b['units_inferred']} -> {f['units_inferred']}")
    for kind in sorted(set(b["kinds"]) | set(f["kinds"])):
        x, y = b["kinds"].get(kind, {}), f["kinds"].get(kind, {})
        print(f"  {kind:<11} lines {x.get('lines', 0):>5} -> {y.get('lines', 0):<5} cost {x.get('cost', 0):>11,.2f} -> "
              f"{y.get('cost', 0):<11,.2f} deposit {x.get('deposit', 0):>10,.2f} -> {y.get('deposit', 0):,.2f}")
    for m in out["merged"]:
        print(f"  merged: {m['distributor']} {m['invoice_number']} (kept #{m['kept']}, dropped {m['dropped']})")
    if out["store_map_seeded"]:
        print(f"  store map: store {out['store_map_seeded']} is this restaurant")
    elif len(out["stores_on_file"]) > 1:
        print(f"  store map NOT seeded: the invoices name several stores {out['stores_on_file']} — map them "
              "(fintech_invoices.set_location) before the next file")
    for fl in out["files"]:
        if fl["conflicts"] or fl["skipped_locations"]:
            print(f"  file {fl['file_id']}: conflicts {fl['conflicts']} skipped {fl['skipped_locations']}")
    if "orphan_lines" in out:
        print(f"  orphaned invoice lines: {out['orphan_lines']}")
    print("APPLIED" if out["applied"] else "Dry run: nothing written. Add --apply to write it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
