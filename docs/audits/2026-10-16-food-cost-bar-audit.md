# Food Cost + Bar & Alcohol audit — 10/16/26

Blind pre-production audit of both modules (8 lenses, lead-verified). Full report:
https://claude.ai/artifact/A9MRwU6bRUFEj4wAPmEfmv. Scores: Food Cost 46, Bar & Alcohol 55. ~165 raw findings → 118 unique (5 P0, 32 P1, 51 P2, 30 P3).

`tests/test_audit_fc_bar_1016.py` holds one strict `xfail` per confirmed defect below, each citing its ID. A fix that resolves one turns its test into an XPASS (a failure): flip it to a normal test in the same commit.

## P0
- **FC-CALC-01** — `cogs._backoffice_components`: Back Office food cost ignores the requested window (returns whatever two counts bracket).
- **FC-CALC-02** — ledger kitchens: opening/closing "counts" are daily snapshots of computed stock, so "actual" food cost is theoretical. (No xfail: needs the live ledger path.)
- **BAR-CALC-01** — `bar/variance.compute` / `_counted_variance`: a product not bought inside the counted span gets $0 theoretical while its sales count as matched → phantom missing alcohol.
- **ING-01, ING-02, ING-04 (+ BAR-CALC-08)** — `backoffice_ap.outside_fintech`: split invoices matched once, invoice numbers matched across distributors, batch payments unmatched, unmatched payments to Fintech distributors added as purchases.
- **SCALE-01** — `backoffice_counts.waste_periods` re-costs every recipe per count period.

## P1 with xfails
FC-CALC-03 / POS-01 (bought vs sold coverage), FC-CALC-04 (credit memo overwrites its invoice), FC-CALC-05 / ING-03 (vendor first-word match), FC-CALC-06 (bar-only count = $0 food), FC-CALC-08 / ING-07 (catch-weight on whole numbers), FC-CALC-09 / ING-08 ($0 and credit lines as prices), FC-CALC-12 (unlogged waste as $0, unit level), ING-05 (DMARC fail treated as unsigned; store-less file vouches), ING-06 (SKU across vendors), ING-09 (SOUR CREAM → Cream verified), ING-10 / SCALE-03 (file marked stored before rows), ING-11 (Fintech credit memo with the invoice's number), BAR-CALC-04 (no-uom case as one can), BAR-CALC-06 (stock-up guard above range top only), BAR-CALC-07 (wine bottle costed as a glass), AI-01 (any % passes), AI-02 ("Why:" never checked as a cause).

## P2 with xfails
BAR-CALC-09 / ING-15 (pack parser: 6/1.75L, 12/750ML, 12/1L), POS-06 (promo / hashdisc classified differently by the archive and the nightly report).

Every other finding (with file:line, root cause, impact and fix) is in the report; the lens working files are kept off-repo.
