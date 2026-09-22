# History

Nothing in this folder is live. It is kept for the record, and so a grep
for an old name finds an explanation instead of nothing.

| Folder / file | What it was | Superseded by |
|---|---|---|
| `ios-audits-2026-09-03/` | Seven iOS audit reports (security, bugs, bottlenecks, logic gaps, streaming, offline, accessibility), remediated the same day in four commits | the code; see the open items below |
| `landing-page-2026-05.html` | The first cavnar.ai landing page, served from the repo root until the Worker was narrowed to `public/` on 2026-09-09 | `public/index.html` |
| `account-directions-2026-08/` | Three Claude Design directions for the Account screen (A grouped, B status-first, C editorial) | the shipped Account tab and `AccountSheetKit.swift` |
| `brand-scratch/` | Claude Design canvases, the seal exploration page and font previews used while the brand was being drawn | `brand/assets/` (the sources) and `static/brand/` (the built outputs) |

## Still open from the iOS audits

Sampled on 2026-09-21 while auditing the repository; each is a real gap
in the app, not a documentation issue.

- 3.2 `AskCavnarView` still computes the chat bubble's `userTextWidth` on every render.
- 4.2 Foreground refresh exists only on Home and Labor.
- 6.4 Reviews, Intel and Marketing have no read cache for offline opens; 6.5 Labor shows no staleness notice.
- 7.4 Four charts written after the audit (`LaborRibbonChart`, `WeekRadarChart`, `WasteLedgerChart`, `RecoverableGaugeChart`) carry no accessibility labels; 7.7 `Color.cavnarInk3(_ contrast:)` is defined and never used.
