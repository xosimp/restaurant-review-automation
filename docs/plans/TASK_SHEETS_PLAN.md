# Task sheets by job code — the plan (no code yet)

Erik's problem, in his words: the other two managers do not do their
opening and closing responsibilities consistently, so he ends up doing it
all himself. He wants to write the task sheet for every job code — cooks,
bussers, servers, bartenders **and managers** — edit it whenever he needs
to, and have each person see *their* sheet for *today* when they log in.
This document is the design. It is meant to fix the problem permanently,
which means the system has to do three things the current checklist does
not: put the right sheet in front of the right person at the right time,
make skipping visible the same day, and keep a record nobody can argue with.

## What exists today (and why it is not enough)

- `task_templates(restaurant_id, role, label, sort_order)` — one flat list
  per job role, no shift, no time, no order of operations.
- `task_completions(template_id, task_date, completed_by)` — a tick per
  template per day, no timestamp of *when in the shift*, no evidence.
- The staff portal shows the list for the employee's job role
  (`memberships.job_role`, else the roster, else the last schedule) and lets
  them tick items. The owner adds and removes labels on web and iOS.
- Nothing says who was **supposed** to do it. Nothing happens when it is
  not done. Nothing distinguishes an opener's list from a closer's.

That last line is the whole problem: a list that everyone can see and
nobody owns gets done by Erik.

## Design principles

1. **A sheet belongs to a shift, not a day.** Opening and closing are
   different lists, and a person on the 4pm–close shift never sees the
   opening list.
2. **Every task has one owner for today**, resolved from the published
   schedule: the opener on that job code owns the opening sheet. If two
   people open on the same code, both see it and either tick clears it,
   with the name kept.
3. **Time is part of the task.** "Walk-in temps logged" is due by 10:30,
   not "today." Overdue is a state, not an opinion.
4. **Skipping is visible the same day**, to the manager on duty and to
   Erik, in the product he already reads (Home, the morning brief, the
   issues list). Not a report he has to open.
5. **Erik writes it, Erik edits it, the system never rewrites it.** Sheets
   are his words. Cavnar AI can *suggest* a starter sheet per job code
   (from its own knowledge of restaurant operations) but only as a draft he
   accepts line by line, the same rule as recipe drafts.
6. **Managers are staff too.** The manager sheet is the one Erik cares
   about most, so it is a job code like any other — and manager
   completions are what the consistency report is built from.
7. **Evidence where it matters.** A line can require a photo or a number
   (fridge temperature, safe count). Not every line — Erik chooses.

## The model

### Sheets
`task_sheets` — one per (restaurant, job code, shift kind), owner-authored.
- `job_code` (matches `memberships.job_role` / roster roles, case-insensitive)
- `shift_kind`: `opening` | `closing` | `mid` | `weekly` | `any`
- `title`, `active`, `version`, `updated_by`, `updated_at`
- `days_of_week` (optional; a Friday-only closing addendum)

### Lines
`task_sheet_lines` — ordered lines on a sheet.
- `label` (his words), `sort_order`, `section` (optional: "Bar", "Line 1")
- `due_offset_min` — minutes after shift start (opening) or before shift
  end (closing) by which it must be done; null = any time in the shift
- `proof`: `none` | `photo` | `number` | `note`, plus `proof_label`
  ("Walk-in °F") and an optional `min`/`max` for numbers (a temp out of
  range flags immediately)
- `critical` — a critical line left undone escalates on its own
- `active` — soft-delete keeps history

Editing a sheet bumps `version`; instances already issued for today keep
the version they were issued with, so a line added at noon does not mark
this morning's opener late.

### Assignments (today's instances)
`task_assignments` — one per (sheet, date, shift instance), created by a
job at the start of each local day from the **published schedule**:
- who: `employee_name`(s) on that job code and shift kind
- `shift_start`, `shift_end` (from the schedule row), `due_at` per line
  computed from them
- `status`: `open` | `done` | `partial` | `missed`
- fallback when there is no schedule: the sheet is assigned to whoever
  logs in on that job code that day, and Erik's console shows it as
  "unassigned — no schedule published," which is itself a finding.

### Completions
`task_line_completions` — one per (assignment, line):
- `completed_by` (the login, not a typed name), `completed_at`
- `proof_value` (number or note), `proof_media_id` (photo via the existing
  `marketing_media` store or a new `task_media`)
- `late` (completed after `due_at`), `flagged` (number out of range)
- immutable once written; an un-tick writes a new row with `undone=1`

### Sign-off
`task_signoffs` — the manager on duty signs the shift's sheets: which
assignments were complete, which were missed, a note. Erik signs off on
the manager sheet. Sign-off is optional to configure, but when it is on,
an unsigned shift is a missed sign-off in the report.

## Who sees what

**Employee (staff portal, web + iOS Staff tab).** On login: today's sheet
for their job code and shift, in order, with due times, a tick per line,
and the proof control where required. Overdue lines are red. Done lines
show who and when. One screen, nothing else on it first.

**Manager on duty.** Their own manager sheet, plus a "floor" view: every
open sheet for this shift with per-line status, so they can see at 10:40
that the opening cook's walk-in temps are not logged and go say so. They
can sign off the shift.

**Erik (owner).** A Task sheets editor under Labor: job code × shift kind
grid, drag-order lines, due offsets, proof, critical. A daily view of
completion by shift and by person. A weekly **consistency report**: per
job code and per person, completion rate, on-time rate, critical misses,
and — the one he needs — the two managers side by side.

## What happens when it is not done

- A **critical** line past due creates an `ops_issues` row (kind
  `task_missed`) routed to the manager on duty, using the existing
  routing and text link. One issue per line per day.
- At shift end, an assignment left `partial` or `missed` is a line in
  Erik's evening summary and on Home under Needs attention: "Closing sheet
  — Bar: 3 of 9 done (Jordan)."
- The morning brief carries yesterday's misses for openers, and the
  intraday pulse can carry today's opening misses at the cutoff time.
- Three misses by the same person in 14 days becomes a **pattern** Erik
  sees once ("Jordan has missed the closing safe count 3 times"), so the
  conversation happens with facts, not recollection.
- Every miss, late tick and proof is in the person's record on the Staff
  screen, next to the operational score — which is where "who is reliable"
  is already decided.

## Where Cavnar AI helps without taking over

- **Starter sheets** per job code, generated once as drafts from the
  restaurant's category and hours (a sports bar's bar-close list is not a
  café's), accepted line by line.
- **Duplicates and gaps** flagged when he edits ("two lines say 'wipe
  down the line'").
- **Consistency read** in Erik's own voice on the weekly report:
  deterministic, from the completions — never a model deciding who was
  lazy.
- **Ask Cavnar AI** can answer "who closed Tuesday and what did they
  skip?" from the same rows.

## How it lands on the schedule

The published schedule already knows who works which job code and when,
and `labor.employee_shifts_from_csv` already reads it per person. Opening
= the earliest shift on that job code that day; closing = the latest.
A shift that spans both (a 10-hour manager day) gets both sheets. When
Erik edits the schedule after publishing, assignments re-resolve for
future days only.

## Phases

1. **Sheets and lines** (owner editor on web, read on iOS): tables, the
   editor, the version rule, migration of today's `task_templates` into
   `any`-shift sheets so nothing he already typed is lost.
2. **Assignment from the schedule + employee view**: the daily job, due
   times, proof controls, the staff portal and iOS Staff tab rebuilt around
   "your sheet today."
3. **Misses that reach someone**: critical → issue; shift-end summary; Home
   and morning-brief lines; the 3-in-14 pattern.
4. **Manager sign-off and the consistency report**: the side-by-side that
   answers Erik's actual question.
5. **Starter sheets from Cavnar AI**, accepted line by line.

Each phase ships on its own and is useful on its own. Phase 1 alone gives
him the editable sheets; phase 2 makes them personal; phase 3 makes
skipping cost something.

## Open questions for Erik (before phase 1)

- His job codes as he says them (so the sheets match his schedule's
  spelling exactly).
- Whether openers and closers are always on the schedule, or whether some
  days are "whoever comes in" — that decides how much the fallback matters.
- Which lines need a photo or a number, and which are critical.
- Whether managers should see each other's sheets (default: the manager
  on duty sees everything for their shift; Erik sees everything).
- Sign-off: does he want the closing manager to sign every night?
