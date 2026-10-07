# POS ticket lines past 90 days: a compressed archive

**Status: Not yet built.** Plan, 10/7/26 (AI cost audit #93). Grounded in the
code at that date; re-derive every reader with the commands beside it before
building. It was not built in the 10/7/26 round because pruning the lines
safely needs every reader routed first (below), and one reader path would
otherwise serve a year-old night as an empty one — a lossy prune, which the
round ruled out.

## Why

`pos_ticket_lines` is the largest table: every sale, comp, void, refund and
discount line RPOWER reports, kept 1,095 days (`RETAIN_POS_ARCHIVE_DAYS`,
`ops._RETENTION_DAYS`) — about 4.6 MB a day per RPOWER restaurant with its
indexes. The production volume is 4.47 GB of the Hobby plan's 5 GB maximum
(grown 10/3/26, ~+50 MB a day). Lines older than ~90 days are read rarely
and in bulk, which is what a compressed file is good at.

## The readers of `pos_ticket_lines` today

`rg -n "pos_ticket_lines" --glob '*.py' --glob '!tests/**'`

| Reader | Window | Past 90 days? |
|---|---|---|
| `rpower._raw_from_archive`, through `rpower._day_rows` → `fetch_business_days` (cogs, the labor normaliser), `fetch_loss_lines` (loss_detection, the DSR service block), `fetch_day_sales` (DSR sales block, `dsr/backfill`) | any business date `pos_archive.ready_dates` says is archived | **Yes.** `ready_dates` answers from `pos_archive_days` (kept 1,095 days), so a pruned day reads as archived and its lines come back empty: a DSR backfill or a cogs range over last year would see $0 nights. |
| `event_intel.gameday._items_by_date` | the item mix of past games of a series and their usual nights | **Yes.** `event_intel.engine.PAST_GAMES_DAYS` = 1,095. |
| `pos_archive._levels_in_use` | `PRICE_LEVEL_LOOKBACK_DAYS` = 60 | no |
| `pos_archive.sync_prices` (the main price level) | all rows, `GROUP BY price_level_id` | no — any recent window answers it |
| `rpower.department_sold` | `SOLD_LOOKBACK_DAYS` = 90 | at the edge (90) |
| `service_performance` (summary, payments) | `MAX_DAYS` = 90 | no |
| `dsr/block_service` | the report's night | no |

The retention registry's floor for the table is 400 days
(`ops._RETENTION_FLOOR_DAYS`), for the people, table and price learners;
`ops._RETENTION_READERS` lists only `service_performance.summary`. The two
"yes" rows are not on the readers list and must be before any window drops.

## Design

**Write before prune.** A monthly job (`pos_lines_archive`, `jobs_registry`
entry + `ops.run_job` in the loop, bounded and resumable with a cursor) takes
each restaurant-month whose last day is older than `POS_LINES_HOT_DAYS` (90)
and writes one gzip JSONL file per restaurant-month on the volume:
`<volume>/archive/pos_ticket_lines/<restaurant_id>/<YYYY-MM>.jsonl.gz`, one
JSON object per line with every column (`pos_archive._LINE_COLS`), sorted by
business date and line id. Written to a `.partial` name, `fsync`ed, read back
and its row count and SHA-256 compared with the table's rows for that month,
then renamed; a manifest row (`pos_lines_archive_files`: restaurant, month,
rows, bytes, sha256, written_at, verified_at) is the record. Only a month
whose manifest row is verified may be pruned.

**Prune through the registry.** `pos_ticket_lines` gets its own window
(`RETAIN_POS_LINES_DAYS`, 90) and a `_RETENTION_ROLLUP` entry
(`pos_archive:archive_months`) so `ops.prune_ledgers` runs the archive for
the months it is about to delete and deletes only verified months — the
same "summarise first" rule `ai_reads` and `shift_facts` use. The floor
(400) is lowered only together with the readers below.

**One reader.** `pos_archive.lines(restaurant_id, provider, dates,
columns)` returns rows for any dates: the table for hot days, the month
files for the rest (a month file read once per call, streamed, filtered by
date). Then:

- `rpower._raw_from_archive` reads through it, and `ready_dates` treats a
  day as archived only when the table or a verified month file holds it;
- `event_intel.gameday._items_by_date` reads through it;
- `ops._RETENTION_READERS` gains both, with their windows, so the test that
  holds readers to windows sees them.

**Backup.** The nightly snapshot is the SQLite file only. The month files are
written once and never change, so they go off-site once each: encrypted with
the backup key (Fernet, as the snapshot copy) to object storage under
`BACKUP_S3_PREFIX` + `pos-lines/`, with their own lifecycle (kept as long as
the 1,095-day window, not the 35-day snapshot rule — a separate prefix and
rule). The local files are the restore artifact for themselves, like the
local snapshot. Lines carry no credentials and no guest contact details
(item, amount, reason, approver id), so the scrub registry has nothing to
take out — say so in `offsite_backup`'s registry, where a test holds every
table to a decision. `docs/ops/RECOVERY.md` gains the restore step: copy the
month files back beside the database before the readers above are trusted.

**Size.** At ~4.6 MB a day in the table, a month of lines is ~140 MB; gzip
JSONL of the same rows is expected at a tenth or less. Measure on a copy of
Simple EJ's database before choosing between the volume (simplest; counts
against the 5 GB) and object storage only (no volume cost; every archive read
is a download, cached per process).

## Order

1. The reader (`pos_archive.lines`) and the two routings, tested against a
   database with a month moved out by hand.
2. The writer, its manifest and verification, and the job — run on
   production with pruning still off; compare the files with the table.
3. The off-site copy and the RECOVERY.md step; a restore drill that restores
   a month file.
4. Pruning on: `RETAIN_POS_LINES_DAYS` = 90, the rollup hook, the floor and
   the readers' list updated together.
