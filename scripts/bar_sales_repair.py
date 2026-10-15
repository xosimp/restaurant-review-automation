"""scripts/bar_sales_repair.py — the Bar's sales side, made right once for a
restaurant after the bar fix round (barfix-1015 W3) deploys. Run by hand on
production (railway ssh), dry run first:

    python3 scripts/bar_sales_repair.py --rid 5            # dry run: says what it would do
    python3 scripts/bar_sales_repair.py --rid 5 --apply    # does it

Three steps, each idempotent (run again: nothing new happens):

1. `--catalog` (with --apply): keep the POS's sales categories and menu now
   (pos_archive.sync_catalog — RPOWER reads, read-only), so lines archived
   before layout 4 are filed by department and item category, and the bar
   mapping's suggestions see RPOWER's is_bar. The 4am archive does this
   nightly anyway.
2. Each archived night's drink sales net of its check discounts, allocated
   per ticket (bar.sales.repair_nights -> bar_sales_nights). Figures only,
   derived from the archive already on file; no POS call.
3. Nights holding net sales but no department dollars — Simple EJ's 9/27/26,
   whose first report froze $11,617.28 as "Unmapped" (bar audit V5 / FIN-3)
   — given their department and item-category dollars from the archive
   (dsr.backfill.repair_department_metrics), only where they add up to the
   night's stored net. `--archive` stores such a night from RPOWER again
   first (needed when its archived lines predate layout 4 and no catalog
   is kept yet).

Sends nothing, calls no model. Prints JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rid", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--catalog", action="store_true", help="read the POS's sales catalog first (RPOWER, read-only)")
    ap.add_argument("--archive", action="store_true", help="re-archive a night that needs it before repairing it")
    ap.add_argument("--date", action="append", help="limit step 3 to these nights (YYYY-MM-DD; repeatable)")
    a = ap.parse_args(argv)
    from bar.sales import repair_nights
    from dsr.backfill import repair_department_metrics
    out = {"rid": a.rid, "applied": bool(a.apply)}
    if a.catalog and a.apply:
        import pos_archive
        out["catalog"] = pos_archive.sync_catalog(a.rid)
    if a.apply:
        out["nights"] = repair_nights(a.rid)
    out["department_repair"] = repair_department_metrics(a.rid, dates=a.date, archive=a.archive,
                                                         dry_run=not a.apply)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
