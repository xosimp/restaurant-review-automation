# Back Office (Buyers Edge Platform) — what connecting it takes

Research note, 21 Sep 2026. Erik (Simple EJ's) runs inventory, invoices and
recipe costing in **Back Office by Buyers Edge Platform** (bepbackoffice.com),
not in Cavnar AI's Food Cost module. This is what we know, what we do not,
and the three ways in, cheapest first.

## What Back Office is

A restaurant back-office and AP-automation product: invoice capture (they
digitise supplier invoices), inventory counts, recipe costing, accounting
export, payroll. Their integrations page lists 29 POS systems — **rPower is
one of them** — plus payroll and benefits providers, and says only: "Reduce
friction with easy-to-implement APIs and support." No public developer
documentation, no public API reference, no published export formats. The
GoTab partner page describes the POS→Back Office direction only (sales and
tax into Back Office's accounting); nothing describes data coming *out*.

Sources: [bepbackoffice.com/integrations](https://bepbackoffice.com/integrations/),
[bepbackoffice.com](https://bepbackoffice.com/),
[GoTab × Buyers Edge](https://gotab.com/integrations/buyers-edge),
[Buyers Edge launch release](https://www.prnewswire.com/news-releases/buyers-edge-platform-launches-back-office-one-stop-shop-for-restaurants-to-manage-food-costs-and-automate-back-office-operations-301763320.html).

## What Cavnar AI would want from it

Back Office already holds the four things our Food Cost module asks the
owner to type or scan:

| Back Office holds | Cavnar table it would feed | What it unlocks |
|---|---|---|
| Supplier invoices, line-priced | `invoice_imports` → ingredient costs | no invoice photos; price-spike alerts on real prices |
| Inventory counts (on-hand by item) | `ingredient_stock_events` (recount) | waste and days-on-hand without the count sheet |
| Recipes (ingredient × qty per plate) | `recipe_ingredients` | plate cost, margins, depletion from RPower item sales |
| Item master (names, units, pack sizes) | `ingredients` | the list every other feature matches against |

RPower already gives us item-level *sales*; Back Office would give us
item-level *cost*. Together they are the whole food-cost picture.

## The three ways in

1. **Scheduled export from Back Office → CSV import into Cavnar.** Back
   Office exports inventory, recipe and invoice data to CSV/Excel for its
   accounting integrations. We already have `recipes.import_csv` (menu
   item, ingredient, qty) and `invoices.scan` (photo/PDF). Adding an
   *ingredient-list* CSV import and an *inventory-count* CSV import is a
   day's work each, and Erik's team can run the export weekly. Zero
   dependence on Buyers Edge. **This is the path to start on** — it needs
   only a sample export from Erik to confirm columns.
2. **Back Office API, as an integration partner.** Their page says the
   APIs exist and are "easy to implement," but access is by arrangement.
   The ask to Buyers Edge is: read scope on items, inventory counts,
   recipes and invoice lines for a member account, bearer or OAuth. Same
   shape as the RPower adapter (`pos.py` provider contract, nightly
   `sync_to_db`, archived locally). Timeline is theirs, not ours — like
   RPower's customer scope.
3. **Invoice PDFs forwarded by email.** Back Office emails or stores the
   digitised invoice; forwarding those to a Cavnar inbox would feed
   `invoices.scan` unchanged. Useful only if (1) is refused.

## Built on Cavnar AI's side (9/26/26)

The seam is in place; only the HTTP calls are missing.
- `inventory_sync.py` — the provider contract (`is_connected`, `connected_ids`, `label`, `fetch_inventory` → items + counts), the engine that writes a provider's rows into `ingredients` (upsert by `external_ref`, then name) and the stock ledger (a `recount` per ingredient and day, `source` = provider), `status()` for the pages, and a failure stamped on `client_data.inventory_sync_error` with nothing written.
- `backoffice.py` — the Back Office provider. `is_connected` needs `BACKOFFICE_API_BASE` and `restaurants.backoffice_api_key`, so it is off everywhere today; `fetch_inventory` raises `BackOfficeNotReady` with the mapping notes.
- Nightly: `scheduler.run_inventory_sync`, after the 3am POS sync, bounded and resumable (`job_cursors` key `inventory_sync_cursor`).
- The page: once a sync lands, the count sheet is read-only ("Counts from Back Office · synced M/D/YY", no Save count), supplier picks are locked, and the price monitor says "Synced from Back Office" with its typing controls gone. Before that, hand entry stays as it is.
- Recipes are in the contract too (9/26/26): `recipes: [{dish, ref, sell_price, lines: [{ref|name, qty}]}]` - each dish upserted into `menu_items` (by `external_ref`, then name) and its `recipe_ingredients` replaced; the web's Recipes block lists them once synced (the menu-paste and recipe-card drafting are off the web).
- Still to write with the docs: the calls in `fetch_inventory`, an admin save + verify route for the key (the `rpower_routes.py` shape), and invoice lines in the contract.

## Built 10/15/26: the bar's inventory valuation by email — UNVERIFIED

`email_intake` kind **`backoffice_valuation`** → `bar.counts.read_valuation_file`: an emailed Back Office "inventory valuation by category" report becomes a bar count (`bar_counts`, `source` backoffice, `method` import) for the counted pour cost on the Bar page. **Built without a sample** — the format below is our assumption, not Back Office's documented one; confirm it against Erik's first real report before relying on it.
- A CSV (or TSV) or XLSX. Within its first 15 rows, a header row naming a **category** column (a header containing category, class, group, GL, account, type or department) and a **value** column (a header containing value or valuation; failing that extended, total, amount or cost — never a unit, each, per, qty, count, par, price or % column).
- Rows are summed into beer (beer, draft, keg, seltzer, cider, ale, lager, IPA), liquor (liquor, spirits, vodka, whiskey, bourbon, tequila, mezcal, rum, gin, scotch, cordials, liqueur, brandy, cognac) and wine (wine, champagne, sparkling, prosecco); total, non-alcoholic, mixer, food and supply rows are ignored and listed.
- The count date: a date column (one date across the rows), else one date in the rows above the header ("Inventory Date: 10/31/2026"; a period "10/1/2026 - 10/31/2026" is read at its end, and the result says so), else one date in the file's name.
- Never guessed: no date, two different dates, a row that reads as two categories ("Beer & Wine"), a negative value, no beer/liquor/wine row, a PDF or anything else is `unrecognized` with the reason, and nothing is saved. A value the owner typed for that date is kept (`kept_owner`).
- To turn it on for a restaurant: `email_intake.new_route(rid, "backoffice_valuation", domain)` and Back Office's scheduled email sent to that address.

## What we need from Erik to start (1)

- One export each of: item list, a recent inventory count, one recipe, and
  one week of invoices — as Back Office produces them (CSV or XLSX).
- Which of those his team already exports for accounting (that cadence is
  the cadence we build to).
- Whether Back Office is mapped to his RPower items by name or by an id we
  can see — that decides whether recipes join to RPower sales
  automatically or need a one-time mapping screen.

## Not yet known, and must not be assumed

- Column names and units in any Back Office export (the bar valuation reader above assumes a category and a value column — unverified).
- Whether counts export as on-hand quantity, value, or both.
- Whether the API exists for members at all, or only for partners.
